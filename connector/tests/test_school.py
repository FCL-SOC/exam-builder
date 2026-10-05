"""School mode, against a real Exam Assistant (server.py) running in this process.

Run: python -m unittest discover connector/tests
"""

import asyncio
import copy
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

CONNECTOR = Path(__file__).resolve().parent.parent
ROOT = CONNECTOR.parent
sys.path.insert(0, str(CONNECTOR))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Exam Assistant itself, on a free port with a throwaway database.
app = load("exam_assistant_app", ROOT / "server.py")
app.logging.disable(app.logging.CRITICAL)
app.Handler.log_message = lambda *args: None
_tmp = Path(tempfile.mkdtemp())
app.Handler.store = app.ExamStore(_tmp / "exams.db")
app.Handler.settings = app.SchoolSettings(_tmp)
_httpd = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=_httpd.serve_forever, daemon=True).start()
EXAM_SERVER = f"http://127.0.0.1:{_httpd.server_address[1]}"

# The connector in school mode (a separate module from test_connector's link-mode one).
os.environ.update({"EXAM_SERVER": EXAM_SERVER, "EDITOR_URL": "http://8801-openai-01:7900/",
                   "DATA_DIR": str(_tmp / "connector")})
connector = load("connector_school_mode", CONNECTOR / "server.py")
for key in ("EXAM_SERVER", "EDITOR_URL", "DATA_DIR"):
    del os.environ[key]

from mcp import Client  # noqa: E402

SAMPLE = json.loads((ROOT / "examples" / "sample_exam.json").read_text(encoding="utf-8"))
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def http(method, path, body=None, **query):
    req = urllib.request.Request(f"{EXAM_SERVER}/api/{path}?{urllib.parse.urlencode(query)}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read())}


def q(text, marks=1, **extra):
    return {"marks": marks, "blocks": [{"type": "text", "value": text}, {"type": "lines", "n": 2}], **extra}


class SchoolModeTests(unittest.TestCase):
    def setUp(self):
        os.environ["STAFF_CODE"] = "ABC"

    def tearDown(self):
        os.environ.pop("STAFF_CODE", None)

    def call(self, tool, **args):
        async def go():
            async with Client(connector.mcp) as c:
                return await c.call_tool(tool, args)
        result = asyncio.run(go())
        if result.is_error:
            return {"tool_error": result.content[0].text}
        return result.structured_content

    def create(self, exam=None):
        r = self.call("create_exam", exam=exam or copy.deepcopy(SAMPLE))
        self.assertTrue(r["ok"], r)
        return r["exam_id"]

    def saved(self, uid, owner="ABC"):
        return http("GET", f"exams/{uid}", owner=owner)

    def ids(self, uid):
        """Section and question ids in order, from the saved exam."""
        ex = self.saved(uid)["exam"]
        return [[s["id"], [qq["id"] for qq in s["questions"]]] for s in ex["sections"]]

    def texts(self, uid, section):
        return [qq["blocks"][0]["value"] for qq in self.saved(uid)["exam"]["sections"][section]["questions"]]

    # ---------------------------------------------------------------- basics
    def test_tools(self):
        async def go():
            async with Client(connector.mcp) as c:
                return [t.name for t in (await c.list_tools()).tools]
        self.assertEqual(asyncio.run(go()), ["get_exam_format", "list_exams", "get_exam", "create_exam", "edit_exam",
                                             "restore_version"])

    def test_staff_code_is_required(self):
        del os.environ["STAFF_CODE"]
        self.assertIn("Settings → Extensions → Exam Assistant", self.call("list_exams")["tool_error"])

    def test_create_saves_into_the_library_with_school_defaults(self):
        http("POST", "settings/pin", {"pin": "1234"})
        req = urllib.request.Request(f"{EXAM_SERVER}/api/settings", method="PUT",
                                     data=json.dumps({"default_task": "End of Unit Test"}).encode(),
                                     headers={"Content-Type": "application/json", "X-Admin-PIN": "1234"})
        urllib.request.urlopen(req).close()
        r = self.call("create_exam", exam=copy.deepcopy(SAMPLE))
        self.assertTrue(r["ok"])
        self.assertEqual(r["editor_link"], f"http://8801-openai-01:7900/#exam={r['exam_id']}")
        self.assertIn("20 marks, 7 questions", r["outline"])
        saved = self.saved(r["exam_id"])
        self.assertEqual((saved["owner"], saved["updated_by"]), ("ABC", "claude"))
        exam = saved["exam"]
        self.assertEqual((exam["task"], exam["total_marks"], exam["shared"]), ("End of Unit Test", 20, False))
        self.assertIn("End of Unit Test", exam["title"])
        self.assertTrue(all(s["id"] and all(qq["id"] for qq in s["questions"]) for s in exam["sections"]))
        listed = self.call("list_exams")["exams"]
        self.assertEqual(listed[0]["exam_id"], r["exam_id"])

    def test_create_rejects_bad_exams(self):
        bad = copy.deepcopy(SAMPLE)
        bad["sections"][0]["questions"][0]["marks"] = None
        r = self.call("create_exam", exam=bad)
        self.assertFalse(r["ok"])
        self.assertIn("needs marks", r["errors"][0])

    def test_get_exam_outline(self):
        uid = self.create()
        r = self.call("get_exam", exam_id=uid)
        s = self.saved(uid)["exam"]["sections"]
        self.assertFalse(r["read_only"])
        self.assertIn(f"Section B: Short answer  [id {s[1]['id']}]", r["outline"])
        part = s[1]["questions"][0]["parts"][1]
        self.assertIn(f"      b. (2 marks) Hence find the $x$-intercepts of the graph of $y = f(x)$.  [id {part['id']}]",
                      r["outline"])
        self.assertIn("   1. (7 marks) Consider the function", r["outline"])
        self.assertIn("There's no exam with id 'nope-nope-nope'", self.call("get_exam", exam_id="nope-nope-nope")["tool_error"])

    # ---------------------------------------------------------------- edits
    def test_add_replace_remove_move(self):
        uid = self.create()
        (a, aq), (b, bq) = self.ids(uid)
        r = self.call("edit_exam", exam_id=uid, changes=[
            {"op": "add", "to": a, "items": [q("new first"), q("new second", id=aq[0])], "after": "start"},
            {"op": "replace", "id": bq[1], "item": q("simultaneous, rewritten", marks=4)},
            {"op": "remove", "id": aq[2]},
        ])
        self.assertTrue(r["ok"], r)
        texts = self.texts(uid, 0)
        self.assertEqual(texts[:2], ["new first", "new second"])
        self.assertEqual(len(texts), 4)
        new_ids = [i for i in self.ids(uid)[0][1]]
        self.assertEqual(len(set(new_ids)), 4)  # the id Claude tried to reuse was replaced
        self.assertEqual(self.ids(uid)[1][1][1], bq[1])  # replace keeps the id
        self.assertEqual(self.saved(uid)["exam"]["total_marks"], 20 - 1 - 3 + 4 + 2)
        self.assertIn("simultaneous, rewritten", r["outline"])

        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "move", "id": bq[1], "to": a, "after": "start"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.texts(uid, 0)[0], "simultaneous, rewritten")

    def test_parts_and_nesting_limits(self):
        uid = self.create()
        (a, aq), (b, bq) = self.ids(uid)
        part_ids = [p["id"] for p in self.saved(uid)["exam"]["sections"][1]["questions"][0]["parts"]]
        r = self.call("edit_exam", exam_id=uid, changes=[
            {"op": "add", "to": part_ids[0], "items": [q("sub i"), q("sub ii")]}])
        self.assertTrue(r["ok"], r)
        sub = self.saved(uid)["exam"]["sections"][1]["questions"][0]["parts"][0]["parts"]
        self.assertEqual([p["blocks"][0]["value"] for p in sub], ["sub i", "sub ii"])
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "add", "to": sub[0]["id"], "items": [q("too deep")]}])
        self.assertIn("can't have parts", r["tool_error"])
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "move", "id": bq[0], "to": aq[0]}])
        self.assertIn("more than two levels", r["tool_error"])

    def test_replace_keeps_the_ids_of_parts_it_keeps(self):
        uid = self.create()
        question = self.saved(uid)["exam"]["sections"][1]["questions"][0]
        kept, dropped = question["parts"][0], question["parts"][1]
        new = {"blocks": question["blocks"], "parts": [kept, q("a brand new part", marks=2, id=dropped["id"] + "x")]}
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "replace", "id": question["id"], "item": new}])
        self.assertTrue(r["ok"], r)
        parts = self.saved(uid)["exam"]["sections"][1]["questions"][0]["parts"]
        self.assertEqual(parts[0]["id"], kept["id"])
        self.assertNotIn(parts[1]["id"], {kept["id"], dropped["id"], dropped["id"] + "x"})

    def test_sections_and_details(self):
        uid = self.create()
        (a, _), (b, _) = self.ids(uid)
        r = self.call("edit_exam", exam_id=uid, changes=[
            {"op": "add_section", "section": {"name": "C", "description": "Extended response",
                                               "questions": [q("Explain.", marks=5)]}},
            {"op": "update_section", "id": b, "changes": {"description": "Short answer questions", "to_answer": 3}},
            {"op": "update_details", "changes": {"unit": "Quadratics", "writing_min": 70, "calculator": "cas"}},
        ])
        self.assertTrue(r["ok"], r)
        exam = self.saved(uid)["exam"]
        self.assertEqual([s["name"] for s in exam["sections"]], ["A", "B", "C"])
        self.assertEqual((exam["sections"][1]["description"], exam["sections"][1]["to_answer"]),
                         ("Short answer questions", 3))
        self.assertEqual((exam["unit"], exam["writing_min"], exam["calculator"], exam["total_marks"]),
                         ("Quadratics", 70, "cas", 25))
        self.assertTrue(exam["title"].startswith("Quadratics"))
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "update_details", "changes": {"calculator": "abacus"}}])
        self.assertFalse(r["ok"])
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "update_section", "id": a, "changes": {"colour": "red"}}])
        self.assertIn("sections only have", r["tool_error"])

    def test_bad_changes_save_nothing(self):
        uid = self.create()
        (a, aq), _ = self.ids(uid)
        before = self.saved(uid)["updated_at"]
        r = self.call("edit_exam", exam_id=uid, changes=[
            {"op": "add", "to": a, "items": [q("fine")]},
            {"op": "add", "to": a, "items": [{"blocks": [{"type": "lines", "n": "five"}]}]}])
        self.assertFalse(r["ok"])
        self.assertIn("is not of type 'integer'", r["errors"][0])
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "add", "to": a, "items": [q("no marks", marks=None)]}])
        self.assertFalse(r["ok"])
        self.assertIn("needs marks", r["errors"][0])
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "remove", "id": "deadbeef"}])
        self.assertIn("no section, question or part has id 'deadbeef'", r["tool_error"])
        self.assertEqual(self.saved(uid)["updated_at"], before)

    def test_teachers_unfinished_questions_dont_block_claude(self):
        uid = self.create()
        exam = self.saved(uid)["exam"]
        exam["sections"][0]["questions"].append({"id": "half", "marks": None, "blocks": [{"type": "text", "value": ""}], "parts": []})
        http("PUT", f"exams/{uid}", exam, owner="ABC")
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "add", "to": exam["sections"][1]["id"], "items": [q("ok")]}])
        self.assertTrue(r["ok"], r)
        self.assertIn("needs marks", r["outline"])  # the teacher can still see it in the outline

    # ---------------------------------------------------------------- working alongside the teacher
    def test_a_save_in_between_is_merged_not_lost(self):
        uid = self.create()
        (a, aq), _ = self.ids(uid)
        real_put, sneaked = connector.school.server.put, []

        def teacher_saves_first(owner, uid_, exam, base):
            if not sneaked:  # the teacher saves just after Claude read the exam
                current = http("GET", f"exams/{uid_}", owner=owner)
                current["exam"]["sections"][0]["questions"][2]["blocks"][0]["value"] = "teacher typed this"
                http("PUT", f"exams/{uid_}", current["exam"], owner=owner, base=current["updated_at"])
                sneaked.append(True)
            return real_put(owner, uid_, exam, base)

        connector.school.server.put = teacher_saves_first
        try:
            r = self.call("edit_exam", exam_id=uid, changes=[{"op": "replace", "id": aq[0], "item": q("Claude's")}])
        finally:
            connector.school.server.put = real_put
        self.assertTrue(r["ok"], r)
        texts = self.texts(uid, 0)
        self.assertEqual((texts[0], texts[2]), ("Claude's", "teacher typed this"))

    def test_shared_exam_of_another_teacher_is_read_only(self):
        http("PUT", "exams/theirs-0001", {"unit": "Shared", "shared": True, "sections": [
            {"id": "s", "name": "A", "questions": [dict(q("x"), id="x1", parts=[])]}]}, owner="XYZ")
        r = self.call("get_exam", exam_id="theirs-0001")
        self.assertTrue(r["read_only"])
        r = self.call("edit_exam", exam_id="theirs-0001", changes=[{"op": "remove", "id": "x1"}])
        self.assertIn("belongs to XYZ", r["tool_error"])

    def test_sharing_setting_survives_edits(self):
        uid = self.create()
        exam = self.saved(uid)["exam"]
        exam["shared"] = True
        http("PUT", f"exams/{uid}", exam, owner="ABC")
        self.call("edit_exam", exam_id=uid, changes=[{"op": "update_details", "changes": {"unit": "Still shared"}}])
        self.assertTrue(self.saved(uid)["exam"]["shared"])

    def test_older_exam_gets_ids_when_read(self):
        http("PUT", "exams/old-0000001", {"unit": "Old", "sections": [
            {"name": "A", "questions": [{"marks": 1, "blocks": [{"type": "lines", "n": 1}], "parts": []}]}]}, owner="ABC")
        r = self.call("get_exam", exam_id="old-0000001")
        qid = self.saved("old-0000001")["exam"]["sections"][0]["questions"][0]["id"]
        self.assertTrue(qid)
        self.assertIn(f"[id {qid}]", r["outline"])

    def test_images_are_never_sent_and_survive(self):
        uid = self.create()
        exam = self.saved(uid)["exam"]
        target = exam["sections"][1]["questions"][1]
        target["blocks"].insert(1, {"type": "image", "value": PNG, "width": 50})
        http("PUT", f"exams/{uid}", exam, owner="ABC")
        r = self.call("get_exam", exam_id=uid)
        self.assertNotIn("data:image", json.dumps(r))
        shown = r["exam"]["sections"][1]["questions"][1]
        ref = shown["blocks"][1]["ref"]
        self.assertRegex(ref, r"^img-[0-9a-f]{8}$")
        rewritten = {**shown, "blocks": [{"type": "text", "value": "Use the diagram."}, shown["blocks"][1], LINES]}
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "replace", "id": shown["id"], "item": rewritten}])
        self.assertTrue(r["ok"], r)
        saved = self.saved(uid)["exam"]["sections"][1]["questions"][1]["blocks"][1]
        self.assertEqual((saved["value"], saved["width"], "ref" in saved), (PNG, 50, False))
        r = self.call("edit_exam", exam_id=uid, changes=[{"op": "add", "to": exam["sections"][1]["id"], "items": [
            {"marks": 1, "blocks": [{"type": "image", "ref": "img-00000000"}, LINES]}]}])
        self.assertIn("no image 'img-00000000'", r["tool_error"])

    # ---------------------------------------------------------------- undo
    def test_restore_version(self):
        uid = self.create()
        (a, aq), _ = self.ids(uid)
        self.call("edit_exam", exam_id=uid, changes=[{"op": "remove", "id": aq[0]}])
        self.call("edit_exam", exam_id=uid, changes=[{"op": "remove", "id": aq[1]}])
        self.assertEqual(len(self.texts(uid, 0)), 1)
        r = self.call("restore_version", exam_id=uid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.texts(uid, 0)), 2)
        self.call("restore_version", exam_id=uid)  # undoing the restore
        self.assertEqual(len(self.texts(uid, 0)), 1)
        self.call("restore_version", exam_id=uid, steps=4)  # restores count as changes too: back before both removals
        self.assertEqual(len(self.texts(uid, 0)), 3)
        self.assertIn("Only", self.call("restore_version", exam_id=uid, steps=30)["tool_error"])


LINES = {"type": "lines", "n": 2}

if __name__ == "__main__":
    unittest.main()
