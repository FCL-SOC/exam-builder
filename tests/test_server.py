"""Run: python -m unittest discover tests   (standard library only)"""

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

server.logging.disable(server.logging.CRITICAL)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = server.ExamStore(self.dir / "exams.db")

    def tearDown(self):
        self.store._conn.close()
        self.tmp.cleanup()

    def test_normalise_owner(self):
        self.assertEqual(server.normalise_owner(" abc "), "ABC")
        for bad in ("", "ab", "abcd", "ab1", None):
            self.assertEqual(server.normalise_owner(bad), "")

    def test_other_teacher_cannot_read_write_or_delete(self):
        self.assertTrue(self.store.save("ABC", "exam-0001", {"title": "Mine"}))
        self.assertIsNone(self.store.get("XYZ", "exam-0001"))
        self.assertFalse(self.store.save("XYZ", "exam-0001", {"title": "Hijack"}))
        self.assertFalse(self.store.delete("XYZ", "exam-0001"))
        self.assertEqual(self.store.get("ABC", "exam-0001")["exam"]["title"], "Mine")
        self.assertEqual(self.store.list_for("XYZ"), [])

    def test_shared_exam_is_readable_but_not_writable(self):
        self.store.save("ABC", "exam-0001", {"title": "S", "shared": True, "learning_area": "Science"})
        self.assertEqual(self.store.get("XYZ", "exam-0001")["owner"], "ABC")
        self.assertFalse(self.store.save("XYZ", "exam-0001", {"title": "x"}))
        self.assertEqual([e["uid"] for e in self.store.shelf("Science")], ["exam-0001"])
        self.assertEqual(self.store.shelf("English"), [])

    def test_lists_show_exam_details(self):
        self.store.save("ABC", "exam-0001", {"title": "T", "unit": "Probability", "assessment_type": "CAT",
                                             "year_level": "10", "total_marks": 42, "shared": True})
        for row in (self.store.list_for("ABC")[0], self.store.shelf()[0]):
            self.assertEqual((row["topic"], row["assessment_type"], row["year_level"], row["total_marks"]),
                             ("Probability", "CAT", "10", 42))

    def test_upsert_keeps_one_row(self):
        for i in range(3):
            self.store.save("ABC", "exam-0001", {"title": f"v{i}"})
        rows = self.store.list_for("ABC")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "v2")

    def test_timestamps_are_strictly_increasing(self):
        with self.store._lock:
            stamps = [self.store._next_timestamp() for _ in range(50)]
        self.assertEqual(stamps, sorted(set(stamps)))

    def test_empty_db_is_neither_backed_up_nor_pruned(self):
        bdir = self.dir / "backups"
        bdir.mkdir()
        old = [bdir / f"exams-2026010{i}-000000-000000.db" for i in range(3)]
        for p in old:
            p.write_bytes(b"old")
        self.assertIsNone(server.backup_on_startup(self.store, bdir, keep=1))
        self.assertTrue(all(p.exists() for p in old))

    def test_backup_rotation_keeps_newest(self):
        self.store.save("ABC", "exam-0001", {"title": "x"})
        bdir = self.dir / "backups"
        for _ in range(3):
            server.backup_on_startup(self.store, bdir, keep=2)
        self.assertEqual(len(list(bdir.glob("exams-*.db"))), 2)

    def test_save_from_an_old_version_is_refused(self):
        v1 = self.store.save("ABC", "exam-0001", {"title": "v1"})
        v2 = self.store.save("ABC", "exam-0001", {"title": "v2"}, base=v1, by="claude")
        self.assertGreater(v2, v1)
        with self.assertRaises(server.Conflict) as caught:
            self.store.save("ABC", "exam-0001", {"title": "stale"}, base=v1)
        self.assertEqual((caught.exception.updated_at, caught.exception.updated_by), (v2, "claude"))
        self.assertEqual(self.store.get("ABC", "exam-0001")["exam"]["title"], "v2")
        self.assertTrue(self.store.save("ABC", "exam-0001", {"title": "forced"}))  # no base: overwrite, as before
        self.assertTrue(self.store.save("ABC", "exam-0002", {"title": "new"}, base="anything"))  # nothing to clash with

    def test_shared_as_editable_others_can_save_but_not_take_over(self):
        self.store.save("ABC", "exam-0001", {"title": "Mine", "shared": True})
        self.assertFalse(self.store.save("XYZ", "exam-0001", {"title": "Theirs"}))  # shared to view only
        self.store.save("ABC", "exam-0001", {"title": "Mine", "shared": True, "shared_edit": True})
        self.assertTrue(self.store.save("XYZ", "exam-0001", {"title": "Edited", "shared": False}))
        found = self.store.get("ABC", "exam-0001")
        self.assertEqual((found["owner"], found["title"], found["updated_by"]), ("ABC", "Edited", "xyz"))
        self.assertEqual((found["exam"]["shared"], found["exam"]["shared_edit"]), (True, True))  # still the owner's choice
        self.assertFalse(self.store.delete("XYZ", "exam-0001"))

    def test_version(self):
        stamp = self.store.save("ABC", "exam-0001", {"title": "x"}, by="claude")
        self.assertEqual(self.store.version("ABC", "exam-0001"), {"updated_at": stamp, "updated_by": "claude"})
        self.assertIsNone(self.store.version("XYZ", "exam-0001"))
        self.store.save("ABC", "exam-0001", {"title": "x"})
        self.assertEqual(self.store.version("ABC", "exam-0001")["updated_by"], "")
        self.assertEqual(self.store.list_for("ABC")[0]["updated_by"], "")

    def test_database_from_before_updated_by_is_migrated(self):
        old = self.dir / "old.db"
        conn = sqlite3.connect(old)
        conn.executescript(server.SCHEMA_TEMPLATE.format(table="exams"))
        conn.execute("INSERT INTO exams (exam_uid, owner, title, created_at, updated_at, body) "
                     "VALUES ('exam-0001', 'ABC', 'T', 't', 't', '{}')")
        conn.commit()
        conn.close()
        store = server.ExamStore(old)
        self.assertEqual(store.version("ABC", "exam-0001"), {"updated_at": "t", "updated_by": ""})
        store._conn.close()
        server.ExamStore(old)._conn.close()  # opening it again doesn't try to add the column twice


class FakeConnector:
    """Something answering /healthz like the Claude connector does."""

    def __enter__(self):
        class Healthy(server.SimpleHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), Healthy)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.httpd.server_address[1]

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StyleGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = server.SchoolSettings(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_built_in_guide_until_the_school_writes_its_own(self):
        self.assertIn("Command terms", server.DEFAULT_STYLE_GUIDE)
        self.assertIn("Learning Intentions", server.DEFAULT_PLAN_GUIDE)
        for key in ("style_guide", "plan_guide"):
            self.assertEqual(self.settings.public()[key], server.DEFAULT_SETTINGS[key])
            self.assertEqual(self.settings.update({key: "Our way."})[key], "Our way.")
            stored = json.loads((Path(self.tmp.name) / "settings.json").read_text())
            self.assertEqual(stored[key], "Our way.")

    def test_empty_or_unchanged_guide_isnt_stored(self):
        """So the school keeps getting improvements to the built-in guides."""
        for key in ("style_guide", "plan_guide"):
            self.settings.update({key: "Our way."})
            for value in ("", "   ", server.DEFAULT_SETTINGS[key]):
                self.settings.update({key: value, "school_name": "Hillview"})
                stored = json.loads((Path(self.tmp.name) / "settings.json").read_text())
                self.assertNotIn(key, stored)
                self.assertEqual(self.settings.public()[key], server.DEFAULT_SETTINGS[key])
            with self.assertRaises(ValueError):
                self.settings.update({key: "x" * 30001})


class ClaudeExtensionTests(unittest.TestCase):
    def test_available_only_while_the_connector_runs(self):
        self.assertFalse(server.ClaudeExtension(port=free_port()).available())
        with FakeConnector() as port:
            self.assertTrue(server.ClaudeExtension(port=port).available())
        self.assertFalse(server.ClaudeExtension(folder=Path(tempfile.mkdtemp()), port=port).available())

    def test_bundle_is_filled_in_for_the_teacher(self):
        ext = server.ClaudeExtension(port=7901)
        with zipfile.ZipFile(BytesIO(ext.build("ABC", "8801-openai-01:7900"))) as bundle:
            self.assertEqual(sorted(bundle.namelist()), ["icon.png", "manifest.json", "server/index.js"])
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertEqual(bundle.read("server/index.js"), (server.EXTENSION_DIR / "server" / "index.js").read_bytes())
        config = manifest["user_config"]
        self.assertEqual((config["staff_code"]["default"], config["server_url"]["default"]),
                         ("ABC", "http://8801-openai-01:7901"))
        self.assertEqual(manifest["server"], json.loads((server.EXTENSION_DIR / "manifest.json").read_text())["server"])

    def test_odd_host_headers_fall_back_to_this_computers_name(self):
        ext = server.ClaudeExtension(port=7901)
        for host, expect in (("10.1.2.3:7900", "http://10.1.2.3:7901"), ("[::1]:7900", "http://[::1]:7901"),
                             ('x"><script>', f"http://{socket.gethostname()}:7901"), (None, f"http://{socket.gethostname()}:7901")):
            with zipfile.ZipFile(BytesIO(ext.build("ABC", host))) as bundle:
                url = json.loads(bundle.read("manifest.json"))["user_config"]["server_url"]["default"]
            self.assertEqual(url, expect, host)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = server.SchoolSettings(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_before_anything_is_saved(self):
        pub = self.settings.public()
        self.assertEqual(pub["school_name"], "Your School")
        self.assertEqual((pub["pin_set"], pub["has_logo"]), (False, False))

    def test_site_login_is_off_until_set_and_checks_name_and_password(self):
        self.assertEqual(self.settings.login_user(), "")
        self.settings.set_login("Staff", "Bella")
        self.assertEqual(self.settings.login_user(), "Staff")
        self.assertTrue(self.settings.check_login("staff ", "Bella"))  # the name ignores case and spaces
        self.assertFalse(self.settings.check_login("Staff", "bella"))
        self.assertNotIn("Bella", (self.dir / "settings.json").read_text())
        cookie = self.settings.session_cookie()
        self.assertTrue(self.settings.session_ok(cookie))
        self.assertFalse(self.settings.session_ok(cookie[:-1] + "0"))
        self.settings.set_login("Staff", "Another")  # a new password signs everyone out
        self.assertFalse(self.settings.session_ok(cookie))
        self.settings.set_login("", "")
        self.assertEqual(self.settings.login_user(), "")

    def test_extra_page_is_off_until_switched_on(self):
        self.assertIs(self.settings.public()["extra_page"], False)
        self.assertIs(self.settings.update({"extra_page": True})["extra_page"], True)
        with self.assertRaises(ValueError):
            self.settings.update({"extra_page": "yes"})

    def test_pin_set_check_and_change(self):
        self.assertTrue(self.settings.set_pin("1234"))
        self.assertTrue(self.settings.check_pin("1234"))
        self.assertFalse(self.settings.check_pin("9999"))
        self.assertFalse(self.settings.set_pin("5678", current="0000"))
        self.assertTrue(self.settings.set_pin("5678", current="1234"))
        self.assertTrue(self.settings.check_pin("5678"))

    def test_short_pin_rejected(self):
        with self.assertRaises(ValueError):
            self.settings.set_pin("12")

    def test_pin_is_not_stored_in_plain_text(self):
        self.settings.set_pin("secret-pin-1234")
        text = (self.dir / "settings.json").read_text()
        self.assertNotIn("secret-pin-1234", text)
        self.assertNotIn("pin_hash", json.dumps(self.settings.public()))

    def test_locks_after_too_many_wrong_pins(self):
        self.settings.set_pin("1234")
        for _ in range(server.PIN_TRIES):
            self.assertFalse(self.settings.check_pin("0000"))
        self.assertEqual(self.settings.check_pin("1234"), "locked")

    def test_update_validates_and_ignores_unknown_keys(self):
        pub = self.settings.update({"school_name": "Hillview College", "body_size": 12, "theme_colour": "#123456", "bogus": 1})
        self.assertEqual((pub["school_name"], pub["body_size"], pub["theme_colour"]), ("Hillview College", 12, "#123456"))
        self.assertNotIn("bogus", pub)
        for bad in ({"body_size": 40}, {"body_size": "12"}, {"theme_colour": "red"}, {"body_font": "Comic Sans MS"}, {"school_name": "x" * 500}):
            with self.assertRaises(ValueError):
                self.settings.update(bad)

    def test_logo_checks_the_file_itself(self):
        self.settings.save_logo("image/png", PNG)
        self.assertTrue(self.settings.public()["has_logo"])
        with self.assertRaises(ValueError):
            self.settings.save_logo("image/png", b"<svg onload=alert(1)>")
        with self.assertRaises(ValueError):
            self.settings.save_logo("image/svg+xml", b"<svg/>")
        self.settings.save_logo("image/jpeg", JPEG)
        self.assertEqual(self.settings.logo_path().suffix, ".jpg")
        self.assertFalse((self.dir / "logo.png").exists())
        self.settings.delete_logo()
        self.assertIsNone(self.settings.logo_path())


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.Handler.store = server.ExamStore(Path(self.tmp.name) / "exams.db")
        server.Handler.plans = server.ExamStore(Path(self.tmp.name) / "exams.db", table="lesson_plans",
                                                details=server._PLAN_DETAILS)
        server.Handler.settings = server.SchoolSettings(Path(self.tmp.name))
        server.Handler.feedback = server.Feedback(Path(self.tmp.name) / "exams.db")
        server.Handler.guard = server.LoginGuard()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.Handler.store._conn.close()
        server.Handler.plans._conn.close()
        server.Handler.feedback._conn.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                content = r.read()
                return r.status, (json.loads(content) if r.headers.get_content_type() == "application/json" else content)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_bad_staff_code_is_400(self):
        self.assertEqual(self.call("GET", "/api/exams")[0], 400)
        self.assertEqual(self.call("GET", "/api/exams?owner=ab1")[0], 400)

    def test_round_trip_and_owner_scoping(self):
        self.assertEqual(self.call("PUT", "/api/exams/exam-0001?owner=abc", {"title": "T"})[0], 200)
        status, got = self.call("GET", "/api/exams/exam-0001?owner=ABC")
        self.assertEqual((status, got["exam"]["title"], got["owner"]), (200, "T", "ABC"))
        self.assertEqual(self.call("GET", "/api/exams/exam-0001?owner=XYZ")[0], 404)
        self.assertEqual(self.call("PUT", "/api/exams/exam-0001?owner=XYZ", {"title": "x"})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/exams/exam-0001?owner=XYZ")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/exams/exam-0001?owner=ABC")[0], 200)

    def test_bad_uid_and_body_rejected(self):
        self.assertEqual(self.call("PUT", "/api/exams/..%2Fescape?owner=ABC", {"a": 1})[0], 400)
        self.assertEqual(self.call("PUT", "/api/exams/exam-0001?owner=ABC", [1, 2])[0], 400)

    def test_saves_carry_versions(self):
        status, first = self.call("PUT", "/api/exams/exam-0001?owner=ABC", {"title": "v1"})
        self.assertEqual(status, 200)
        v1 = urllib.parse.quote(first["updated_at"])
        status, second = self.call("PUT", f"/api/exams/exam-0001?owner=ABC&base={v1}&by=claude", {"title": "v2"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/exams/exam-0001/version?owner=ABC"),
                         (200, {"updated_at": second["updated_at"], "updated_by": "claude"}))
        status, clash = self.call("PUT", f"/api/exams/exam-0001?owner=ABC&base={v1}", {"title": "stale"})
        self.assertEqual((status, clash["updated_at"], clash["updated_by"]), (409, second["updated_at"], "claude"))
        self.assertEqual(self.call("GET", "/api/exams/exam-0001?owner=ABC")[1]["exam"]["title"], "v2")
        self.assertEqual(self.call("GET", "/api/exams/exam-0001/version?owner=XYZ")[0], 404)
        self.assertEqual(self.call("PUT", "/api/exams/exam-0001?owner=ABC&by=Robot!", {"title": "x"})[0], 400)

    def test_claude_extension_download(self):
        server.Handler.claude = server.ClaudeExtension(port=free_port())
        self.assertEqual(self.call("GET", "/claude/status"), (200, {"available": False}))
        self.assertEqual(self.call("GET", "/claude/exam-assistant.mcpb?owner=ABC")[0], 404)
        with FakeConnector() as port:
            server.Handler.claude = server.ClaudeExtension(port=port)
            self.assertEqual(self.call("GET", "/claude/status"), (200, {"available": True}))
            self.assertEqual(self.call("GET", "/claude/exam-assistant.mcpb?owner=a1")[0], 400)
            req = urllib.request.Request(self.base + "/claude/exam-assistant.mcpb?owner=xyz", headers={"Host": "examserver:7900"})
            with urllib.request.urlopen(req) as r:
                self.assertIn("exam-assistant.mcpb", r.headers["Content-Disposition"])
                manifest = json.loads(zipfile.ZipFile(BytesIO(r.read())).read("manifest.json"))
        self.assertEqual(manifest["user_config"]["staff_code"]["default"], "XYZ")
        self.assertEqual(manifest["user_config"]["server_url"]["default"], f"http://examserver:{port}")
        server.Handler.claude = server.ClaudeExtension()

    def test_lesson_plans_are_stored_like_exams_but_separately(self):
        plan = {"class_code": "10MM1", "subject": "Mathematics", "topic": "Quadratics", "title": "Quadratics",
                "sections": {"L": "**Learning Intentions**", "E": "", "A": "", "R": "", "N": ""}}
        status, first = self.call("PUT", "/api/plans/plan-0001?owner=ABC", plan)
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/exams?owner=ABC")[1], [])  # not mixed in with exams
        rows = self.call("GET", "/api/plans?owner=ABC")[1]
        self.assertEqual((rows[0]["uid"], rows[0]["class_code"], rows[0]["topic"]), ("plan-0001", "10MM1", "Quadratics"))
        v1 = urllib.parse.quote(first["updated_at"])
        self.assertEqual(self.call("PUT", f"/api/plans/plan-0001?owner=ABC&base={v1}&by=claude", plan)[0], 200)
        self.assertEqual(self.call("PUT", f"/api/plans/plan-0001?owner=ABC&base={v1}", plan)[0], 409)
        self.assertEqual(self.call("GET", "/api/plans/plan-0001/version?owner=ABC")[1]["updated_by"], "claude")
        self.assertEqual(self.call("GET", "/api/plans/plan-0001?owner=XYZ")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/plans/plan-0001?owner=ABC")[0], 200)
        self.assertEqual(self.call("PUT", "/api/nonsense/plan-0001?owner=ABC", plan)[0], 400)

    def test_serves_frontend(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Exam Assistant", r.read())

    def test_site_login_keeps_out_everyone_without_it(self):
        server.Handler.trust_local = False  # these requests come from this computer, which is trusted otherwise
        self.addCleanup(setattr, server.Handler, "trust_local", True)
        self.assertEqual(self.call("GET", "/api/settings")[0], 200)  # no login set: open as before
        server.Handler.settings.set_login("Staff", "Bella")
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

        def get(path):
            try:
                with opener.open(self.base + path) as r:
                    return r.status, r.url, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.url, e.read()

        def login(user, password, nxt="/?exam=abc12345"):
            data = urllib.parse.urlencode({"user": user, "password": password, "next": nxt}).encode()
            try:
                with opener.open(self.base + "/login", data) as r:
                    return r.status, r.url
            except urllib.error.HTTPError as e:
                return e.code, e.url

        status, url, body = get("/?exam=abc12345")
        self.assertEqual((status, urllib.parse.urlparse(url).path), (200, "/login"))  # sent to the login page
        self.assertIn(b"Staff log in", body)
        self.assertEqual(get("/api/exams?owner=ABC")[0], 401)
        self.assertEqual(login("Staff", "wrong")[0], 401)
        self.assertEqual(login("Staff", "Bella", nxt="//evil.example/")[1], self.base + "/")  # never off-site
        self.assertEqual(get("/api/exams?owner=ABC")[0], 200)  # logged in
        self.assertEqual(get("/logout")[0], 200)
        self.assertEqual(get("/api/exams?owner=ABC")[0], 401)
        server.Handler.trust_local = True  # the Claude connector, on the server itself, needs no login
        self.assertEqual(get("/api/exams?owner=ABC")[0], 200)

    def test_site_login_locks_out_a_computer_after_five_wrong_passwords(self):
        server.Handler.trust_local = False
        self.addCleanup(setattr, server.Handler, "trust_local", True)
        server.Handler.settings.set_login("Staff", "Bella")
        def login(password):
            data = urllib.parse.urlencode({"user": "Staff", "password": password}).encode()
            try:
                with urllib.request.urlopen(self.base + "/login", data) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                with e:
                    return e.code
        for _ in range(server.LoginGuard.FREE_TRIES):
            self.assertEqual(login("guess"), 401)
        self.assertEqual(login("Bella"), 429)  # locked out, even with the right password

    def test_feedback_is_kept_and_read_with_the_admin_pin(self):
        self.assertEqual(self.call("POST", "/api/feedback?owner=abc", {"text": "Add network diagrams", "page": "exams"})[0], 200)
        self.assertEqual(self.call("POST", "/api/feedback", {"text": "Anonymous idea"})[0], 200)
        self.assertEqual(self.call("POST", "/api/feedback", {"text": "  "})[0], 400)
        self.call("POST", "/api/settings/pin", {"pin": "1234"})
        self.assertEqual(self.call("GET", "/api/settings/feedback")[0], 403)
        status, rows = self.call("GET", "/api/settings/feedback", headers={"X-Admin-PIN": "1234"})
        self.assertEqual((status, [(r["owner"], r["text"]) for r in rows]), (200, [("", "Anonymous idea"), ("ABC", "Add network diagrams")]))

    def test_settings_need_the_admin_pin(self):
        status, pub = self.call("GET", "/api/settings")
        self.assertEqual((status, pub["school_name"], pub["pin_set"]), (200, "Your School", False))
        self.assertEqual(self.call("PUT", "/api/settings", {"school_name": "X"}, {"X-Admin-PIN": "1234"})[0], 409)
        self.assertEqual(self.call("POST", "/api/settings/pin", {"pin": "12"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings/pin", {"pin": "1234"})[0], 200)
        self.assertEqual(self.call("POST", "/api/settings/pin", {"pin": "9999"})[0], 403)  # changing it needs the current PIN
        self.assertEqual(self.call("PUT", "/api/settings", {"school_name": "X"})[0], 403)
        self.assertEqual(self.call("PUT", "/api/settings", {"body_size": 99}, {"X-Admin-PIN": "1234"})[0], 400)
        status, pub = self.call("PUT", "/api/settings", {"school_name": "Hillview College"}, {"X-Admin-PIN": "1234"})
        self.assertEqual((status, pub["school_name"]), (200, "Hillview College"))
        self.assertEqual(self.call("POST", "/api/settings/unlock", {"pin": "1234"})[0], 200)
        self.assertNotIn("pin_hash", json.dumps(self.call("GET", "/api/settings")[1]))

    def test_logo_upload_and_serving(self):
        self.call("POST", "/api/settings/pin", {"pin": "1234"})
        self.assertEqual(self.call("GET", "/school-logo")[0], 404)
        self.assertEqual(self.call("PUT", "/api/settings/logo", raw=PNG, headers={"Content-Type": "image/png"})[0], 403)
        self.assertEqual(self.call("PUT", "/api/settings/logo", raw=b"<svg/>", headers={"Content-Type": "image/svg+xml", "X-Admin-PIN": "1234"})[0], 400)
        self.assertEqual(self.call("PUT", "/api/settings/logo", raw=PNG, headers={"Content-Type": "image/png", "X-Admin-PIN": "1234"})[0], 200)
        status, content = self.call("GET", "/school-logo")
        self.assertEqual((status, content), (200, PNG))
        self.assertTrue(self.call("GET", "/api/settings")[1]["has_logo"])
        self.assertEqual(self.call("DELETE", "/api/settings/logo", headers={"X-Admin-PIN": "1234"})[0], 200)
        self.assertEqual(self.call("GET", "/school-logo")[0], 404)


if __name__ == "__main__":
    unittest.main()
