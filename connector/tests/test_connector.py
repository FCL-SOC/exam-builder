"""Run: python -m unittest discover connector/tests   (needs connector/requirements.txt installed)"""

import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CONNECTOR = Path(__file__).resolve().parent.parent
ROOT = CONNECTOR.parent
sys.path.insert(0, str(CONNECTOR))
_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
os.environ["PUBLIC_URL"] = "https://exams.example.org"
os.environ["SCHOOL_URL"] = "http://examserver:7900/"

import exam_format  # noqa: E402
import server  # noqa: E402
from mcp import Client  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

SAMPLE = json.loads((ROOT / "examples" / "sample_exam.json").read_text(encoding="utf-8"))


def exam(*questions, **cover):
    return {**cover, "sections": [{"name": "A", "questions": list(questions)}]}


def q(*blocks, marks=1, parts=None):
    item = {"marks": marks, "blocks": list(blocks)}
    if parts is not None:
        item["parts"] = parts
    return item


LINES = {"type": "lines", "n": 3}


def graph(*exprs, **extra):
    return {"type": "graph", "x": {"min": -5, "max": 5}, "y": {"min": -5, "max": 5},
            "functions": [{"expr": e} for e in exprs], **extra}


class ValidateTests(unittest.TestCase):
    def check(self, ex):
        return exam_format.validate(ex)

    def assertError(self, ex, pattern):
        r = self.check(ex)
        self.assertFalse(r["ok"], r)
        self.assertTrue(any(re.search(pattern, e) for e in r["errors"]), r["errors"])

    def assertWarning(self, ex, pattern):
        r = self.check(ex)
        self.assertTrue(r["ok"], r)
        self.assertTrue(any(re.search(pattern, w) for w in r["warnings"]), r["warnings"])

    def test_sample_is_clean(self):
        r = self.check(SAMPLE)
        self.assertEqual((r["ok"], r["errors"], r["warnings"]), (True, [], []))
        self.assertEqual((r["total_marks"], r["questions"]), (20, 7))

    def test_not_an_object_or_too_big(self):
        self.assertError([], "JSON object")
        big = q({"type": "text", "value": "y" * 4900}, LINES)
        self.assertError({"sections": [{"name": "A", "questions": [big] * 60}] * 2}, "KB is over")

    def test_schema_messages_point_at_the_problem(self):
        self.assertError(exam(q({"type": "text", "vale": "hi"}, LINES)), r"questions\[0\]\.blocks\[0\]: .*'vale' was unexpected")
        self.assertError(exam(q({"type": "lines", "n": "3"})), r"blocks\[0\]\.n: '3' is not of type 'integer'")
        self.assertError(exam(q({"type": "photo"})), r"blocks\[0\]\.type")
        self.assertError({"sections": []}, "non-empty")
        self.assertError(exam(q(LINES), assessment_type="Mid-year"), "assessment_type")

    def test_bad_choice_option_reports_the_graph_not_the_string(self):
        r = self.check(exam(q({"type": "choices", "options": ["a", {"type": "graph", "x": {"min": 0}}]})))
        self.assertIn("sections[0].questions[0].blocks[0].options[1].x: 'max' is a required property", r["errors"])

    def test_marks_on_leaves(self):
        self.assertError(exam(q(LINES, marks=None)), "needs marks")
        self.assertError(exam(q(LINES, marks=0)), "needs marks")
        self.assertError(exam(q(marks=None, parts=[q(LINES), q(LINES, marks=None)])), r"parts\[1\]: needs marks")
        self.assertWarning(exam(q(marks=5, parts=[q(LINES), q(LINES)])), "own marks are ignored")
        self.assertEqual(self.check(exam(q(marks=None, parts=[q(LINES, marks=2), q(LINES, marks=1.5)])))["total_marks"], 3.5)

    def test_parts_nest_two_deep_only(self):
        sub = q(LINES)
        self.assertTrue(self.check(exam(q(marks=None, parts=[q(marks=None, parts=[sub])])))["ok"])
        self.assertError(exam(q(marks=None, parts=[q(marks=None, parts=[q(marks=None, parts=[sub])])])), "parts")

    def test_answer_space_warning(self):
        self.assertWarning(exam(q({"type": "text", "value": "Explain."})), "no space to answer")
        self.assertTrue(self.check(exam(q({"type": "table", "rows": [["x", "1"], ["y", ""]]})))["warnings"] == [])

    def test_dollars(self):
        self.assertError(exam(q({"type": "text", "value": "It costs $5."}, LINES)), "unpaired \\$")
        self.assertTrue(self.check(exam(q({"type": "text", "value": r"It costs \$5 or $\$6$ and $x^2$."}, LINES)))["ok"])
        self.assertError(exam(q({"type": "equation", "value": "$x^2$"}, LINES)), "without \\$ delimiters")
        self.assertError(exam(q({"type": "table", "rows": [["$5", ""]]})), r"rows\[0\]\[0\]")

    def test_tables(self):
        self.assertError(exam(q({"type": "table", "rows": [["a", "b"], ["c"]]}, LINES)), "same number of cells")
        self.assertError(exam(q({"type": "table", "rows": [["a", ""]], "shade_cols": [2]})), "shade_cols")

    def test_choices(self):
        self.assertError(exam(q({"type": "choices", "options": ["a", "b"], "correct": 2})), "only 2 options")
        self.assertError(exam(q({"type": "choices", "options": ["a", " "], "correct": 0})), "is empty")
        self.assertWarning(exam(q({"type": "choices", "options": ["a", "b"]})), "no correct option")

    def test_graph_expressions(self):
        self.assertTrue(self.check(exam(q(graph("x^2 - 2", "3sin(2x)", "e^(-x)", "1/(x-1)", "sqrt(x+1)")))) ["ok"])
        self.assertError(exam(q(graph("2t + 1"))), "I don't understand \"t\"")
        self.assertError(exam(q(graph("(x + 1"))), "missing its \\)")
        self.assertError(exam(q(graph("sqrt(-1 - x^2)"))), "undefined everywhere")
        self.assertWarning(exam(q(graph("x + 100"))), "never comes within")
        self.assertError(exam(q(graph() | {"functions": [{"expr": "x", "from": 6, "to": 9}]})), "outside the x axis")

    def test_graph_axes_and_kinds(self):
        self.assertError(exam(q(graph() | {"x": {"min": 3, "max": 1}})), r"\.x: min must be less than max")
        self.assertWarning(exam(q(graph() | {"x": {"min": 0, "max": 1000, "step": 1}})), "200 grid steps")
        self.assertError(exam(q({"type": "graph", "x": {"min": 0, "max": 10}})), "needs a 'y' axis")
        box = {"type": "graph", "kind": "boxplot", "x": {"min": 0, "max": 20}}
        self.assertTrue(self.check(exam(q(box | {"box": {"data": "1 2 3 4 5"}})))["ok"])
        self.assertTrue(self.check(exam(q(box | {"box": {"min": 1, "q1": 2, "median": 3, "q3": 4, "max": 5}})))["ok"])
        self.assertTrue(self.check(exam(q(box | {"box": {"hidden": True}})))["ok"])
        self.assertError(exam(q(box | {"box": {"min": 1, "q1": 4, "median": 3, "q3": 4, "max": 5}})), "min ≤ q1")
        self.assertError(exam(q(box)), "needs 'box'")
        hist = {"type": "graph", "kind": "histogram", "x": {"min": 0, "max": 10}, "y": {"min": 0, "max": 10}}
        self.assertTrue(self.check(exam(q(hist | {"hist": {"start": 0, "width": 2, "counts": "1, 2, 3"}})))["ok"])
        self.assertError(exam(q(hist | {"hist": {"start": 0, "width": 2}})), "bar heights")

    def test_sections(self):
        two = {"name": "A", "to_answer": 3, "questions": [q(LINES), q(LINES)]}
        self.assertError({"sections": [two]}, "to_answer")
        self.assertWarning({"sections": [{"name": "A", "questions": [q(LINES)]}] * 2}, "repeat")


class ExpressionParityTests(unittest.TestCase):
    """The Python port must accept, reject and evaluate exactly as the editor's compileExpr does."""

    EXPRS = ["x^2 - 2", "3sin(2x)", "(x+1)(x-1)", "sqrt(x+1)", "e^(-x)", "1/(x-1)", "ln(x)", "log(x)", "y = 2x+1",
             "f(x)=x^3-3x", "xsinx", "2^-x", "-x^2", "2^3^2", "asin(x)", "acos(x/2)", "atan(x)", "exp(x)", "abs(x-1)",
             "π x", "2πx", "x×2", "3·x", "x − 1", "t+1", "2*/x", "(x+1", "x+1)", "", "sin", "x^", "sqrt(-1)",
             "ln(0)", "(-8)^(1/3)", "0^0", "x/0", "1.5x", ".5x", "2(3)", "e", "ee", "logx", "sin x cos x", "--x",
             "+x", "x+-1", "3x^2/2", "-(x-1)^2-2", "x^-1"]
    XS = [-3.7, -2, -1, -0.5, 0, 0.25, 0.5, 1, 2, 3.3]

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_matches_editor(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = re.search(r"^const FUNCS = .*?(?=^function graphLayout)", html, re.S | re.M).group(0)
        script = js + f"""
const out = {{}};
for (const e of {json.dumps(self.EXPRS)}) {{
  try {{ const f = compileExpr(e); out[e] = {{ ys: {json.dumps(self.XS)}.map(x => {{ const y = f(x); return Number.isFinite(y) ? y : null; }}) }}; }}
  catch (err) {{ out[e] = {{ error: err.message }}; }}
}}
console.log(JSON.stringify(out));"""
        editor = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        for e, want in editor.items():
            with self.subTest(expr=e):
                try:
                    fn = exam_format.compile_expr(e)
                except exam_format.ExprError as err:
                    self.assertEqual({"error": str(err)}, want)
                    continue
                self.assertNotIn("error", want)
                for x, y in zip(self.XS, want["ys"]):
                    got = fn(x)
                    if y is None:
                        self.assertFalse(abs(got) < float("inf"), (x, got))
                    else:
                        self.assertAlmostEqual(got, y, places=9)


class PackTests(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        r = exam_format.validate(SAMPLE)
        n = exam_format.normalise(SAMPLE, r["total_marks"])
        self.assertEqual(exam_format.unpack(exam_format.pack(n)), n)
        self.assertEqual(n["total_marks"], 20)
        self.assertFalse(n["shared"])
        self.assertNotIn("task", n)  # the school's default task wording applies
        q1 = n["sections"][1]["questions"][0]
        self.assertIsNone(q1["marks"])
        g = q1["parts"][2]["blocks"][1]
        self.assertEqual((g["grid"], g["numbers"], g["x"]["step"], g["y"]["label"]), (True, True, 1, "y"))
        option = n["sections"][0]["questions"][1]["blocks"][1]["options"][0]
        self.assertEqual(option["width"], 100)
        self.assertEqual(option["functions"][0], {"from": None, "to": None, "dashed": False, "expr": "(x + 1)^2 - 2"})
        self.assertEqual(SAMPLE["sections"][1]["questions"][0]["parts"][2]["blocks"][1].get("grid"), None)  # not mutated

    def test_title_matches_editor(self):
        self.assertEqual(exam_format.exam_title({"unit": "Probability", "subject": "Maths", "task": "Test",
                                                 "semester": "2", "year": "2026"}), "Probability Maths · Test S2 2026")
        self.assertEqual(exam_format.exam_title({"semester": "1", "year": "2026"}), "S1 2026")


class ServerTests(unittest.TestCase):
    def call(self, tool, args=None):
        async def go():
            async with Client(server.mcp) as c:
                return await c.call_tool(tool, args or {})
        return asyncio.run(go())

    def test_tools_listed(self):
        async def go():
            async with Client(server.mcp) as c:
                return [t.name for t in (await c.list_tools()).tools], [p.name for p in (await c.list_prompts()).prompts]
        tools, prompts = asyncio.run(go())
        self.assertEqual(tools, ["get_exam_format", "check_exam", "create_exam_link"])
        self.assertEqual(prompts, ["write_exam"])

    def test_format_has_guide_example_and_schema(self):
        text = self.call("get_exam_format").content[0].text
        for needle in ("# Exam format", "## Complete example", "Functions and Statistics", "## JSON Schema", "\\\\$12.50"):
            self.assertIn(needle, text)

    def test_link_flow(self):
        bad = copy.deepcopy(SAMPLE)
        bad["sections"][0]["questions"][0]["marks"] = None
        r = self.call("create_exam_link", {"exam": bad}).structured_content
        self.assertFalse(r["ok"])
        self.assertIsNone(r["link"])

        r = self.call("create_exam_link", {"exam": SAMPLE}).structured_content
        self.assertTrue(r["ok"])
        self.assertRegex(r["link"], r"^https://exams\.example\.org/e/[A-Za-z0-9_-]{12}$")
        path = r["link"].removeprefix("https://exams.example.org")

        web = TestClient(server.app, base_url="http://localhost")
        page = web.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn("20 marks · 7 questions", page.text)
        hrefs = re.findall(r'href="([^"]+)#data=([A-Za-z0-9_-]+)"', page.text)
        self.assertEqual([h for h, _ in hrefs], ["http://examserver:7900/", "https://fcl-soc.github.io/exam-builder/"])
        opened = exam_format.unpack(hrefs[0][1])
        self.assertEqual(opened["total_marks"], 20)
        self.assertEqual(opened["sections"][0]["questions"][0]["blocks"][1]["correct"], 1)
        self.assertEqual(web.get("/e/nope").status_code, 404)
        self.assertEqual(web.get("/healthz").json()["ok"], True)

    def test_allowed_hosts_include_this_machines_addresses(self):
        import socket
        hosts = server.allowed_hosts()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(("10.254.254.254", 9))
                ip = probe.getsockname()[0]
            except OSError:
                ip = "127.0.0.1"
        self.assertIn(f"{ip}:{server.PORT}", hosts)  # teachers who reach the server by IP
        self.assertIn("exams.example.org", hosts)    # PUBLIC_URL's host
        self.assertNotIn("evil.example", hosts)

    def test_rate_limit(self):
        limiter = server.RateLimit(None, limit=2, window=60)
        sent = []

        async def app(scope, receive, send):
            sent.append("app")

        async def send(msg):
            if msg["type"] == "http.response.start":
                sent.append(msg["status"])

        limiter.app = app
        scope = {"type": "http", "path": "/mcp", "method": "POST", "client": ("1.2.3.4", 1), "headers": []}
        for _ in range(3):
            asyncio.run(limiter(scope, None, send))
        self.assertEqual(sent, ["app", "app", 429])


if __name__ == "__main__":
    unittest.main()
