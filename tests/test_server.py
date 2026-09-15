"""Run: python -m unittest discover tests   (standard library only)"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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
        server.Handler.settings = server.SchoolSettings(Path(self.tmp.name))
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.Handler.store._conn.close()
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

    def test_serves_frontend(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Exam Assistant", r.read())

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
