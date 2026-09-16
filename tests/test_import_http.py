"""The import endpoints. Run: python -m unittest discover tests"""

import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importer  # noqa: E402
import server  # noqa: E402

server.logging.disable(server.logging.CRITICAL)

GOOD = ("ref,kind,content,marks,options,answer,params,image,graph,notes\n"
        "A,section,Short answer,,,,,,,\n"
        ",question,A trolley accelerates.,4,,,,,,\n"
        ",part,Find the force.,2,,,,,,\n"
        ",lines,,,,,lines=4,,,\n").encode()

BAD = ("ref,kind,content,marks,options,answer,params,image,graph,notes\n"
       ",wobble,nonsense,,,,,,,\n").encode()


class ImportHttpTests(unittest.TestCase):
    def setUp(self):
        import tempfile
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

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.headers, r.read()

    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_template_downloads_as_a_real_workbook(self):
        status, headers, body = self.get("/import-template.xlsx")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("spreadsheetml", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertIsNone(zf.testzip())

    def test_template_also_downloads_as_csv(self):
        status, headers, body = self.get("/import-template.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn(b"kind", body)

    def test_upload_returns_an_exam_without_saving_it(self):
        status, body = self.post("/api/import?owner=ABC", GOOD, {"X-Filename": "q.csv"})
        self.assertEqual(status, 200)
        exam = body["exam"]
        self.assertEqual(len(exam["sections"][0]["questions"]), 1)
        self.assertEqual(exam["sections"][0]["questions"][0]["parts"][0]["marks"], 2)
        # the import itself stores nothing: the browser saves it like any new exam
        self.assertEqual(server.Handler.store.list_for("ABC"), [])

    def test_a_bad_sheet_is_400_with_the_row_numbers(self):
        status, body = self.post("/api/import?owner=ABC", BAD, {"X-Filename": "q.csv"})
        self.assertEqual(status, 400)
        self.assertTrue(any("Row 2" in p for p in body["problems"]))

    def test_rubbish_is_refused_not_crashed(self):
        status, body = self.post("/api/import?owner=ABC", b"\x00\x01\x02not a sheet", {"X-Filename": "q.xlsx"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_import_needs_a_staff_code(self):
        self.assertEqual(self.post("/api/import", GOOD)[0], 400)
        self.assertEqual(self.post("/api/import?owner=ab1", GOOD)[0], 400)

    def test_the_downloaded_template_can_be_uploaded_again(self):
        _, _, template = self.get("/import-template.xlsx")
        status, body = self.post("/api/import?owner=ABC", template, {"X-Filename": "t.xlsx"})
        self.assertEqual(status, 200)
        self.assertTrue(body["exam"]["sections"][0]["questions"])
        self.assertTrue(body["warnings"])  # the example has an image placeholder to fill in


if __name__ == "__main__":
    unittest.main()
