"""
Live updates in a real browser: an exam open in the editor picks up changes saved elsewhere (Claude, another tab)
and merges them with the teacher's own unsaved typing.

Needs Playwright and Chromium, like browser_check.py:

    python tests/live_check.py

"Claude" here is the same version-checked API call the connector makes. Exits non-zero on any failure.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import free_port  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


class Api:
    def __init__(self, base, owner):
        self.base, self.owner = base, owner

    def call(self, method, path, body=None, **query):
        query = {"owner": self.owner, **query}
        req = urllib.request.Request(f"{self.base}/api/{path}?{urllib.parse.urlencode(query)}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, uid):
        return self.call("GET", f"exams/{uid}")[1]

    def claude_edit(self, uid, change):
        """Read, change, save with base; retry on conflict — what the connector does."""
        for _ in range(5):
            current = self.get(uid)
            exam = current["exam"]
            change(exam)
            status, _ = self.call("PUT", f"exams/{uid}", exam, base=current["updated_at"], by="claude")
            if status == 200:
                return
        raise RuntimeError("Claude's edit kept clashing")


def question(id, text, marks=1):
    return {"id": id, "marks": marks, "blocks": [{"type": "text", "value": text}, {"type": "lines", "n": 2}], "parts": []}


def texts(exam):
    return [q["blocks"][0]["value"] for s in exam["sections"] for q in s["questions"]]


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    for name in ("server.py", "importer.py"):
        shutil.copy(ROOT / name, tmp)
    shutil.copytree(ROOT / "static", tmp / "static")
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(tmp / "server.py"), str(port)], cwd=tmp,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    api = Api(base, "ABC")
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/settings")
            break
        except OSError:
            time.sleep(0.1)

    uid = "live-exam-0001"
    api.call("PUT", f"exams/{uid}", {"unit": "Quadratics", "subject": "Maths", "sections": [
        {"id": "s1", "name": "A", "description": "", "to_answer": None, "instructions": "",
         "questions": [question("q1", "one"), question("q2", "two"), question("q3", "three")]}]})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context()
            ctx.add_init_script("localStorage.setItem('staffCode', 'ABC')")
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/#exam={uid}")
            page.wait_for_selector(".qhead")
            paper = lambda: page.inner_text("#paper")  # noqa: E731
            status = lambda: page.inner_text("#status")  # noqa: E731

            # 1. Claude changes a question and adds one: shows up live, highlighted.
            def add_and_change(exam):
                exam["sections"][0]["questions"][1]["blocks"][0]["value"] = "two, rewritten by Claude"
                exam["sections"][0]["questions"].append(question("q4", "four, added by Claude", 2))
            started = time.time()
            api.claude_edit(uid, add_and_change)
            page.wait_for_function("document.querySelector('#paper').innerText.includes('four, added by Claude')",
                                   timeout=6000)
            check("Claude's change appears live", "two, rewritten by Claude" in paper(), paper()[:200])
            check(f"within 3 s ({time.time() - started:.1f} s)", time.time() - started < 3.5)
            check("changed questions flash", page.locator("#paper .remote-change").count() == 2,
                  str(page.locator("#paper .remote-change").count()))
            check("status says Claude", status() == "Updated by Claude", status())
            check("totals updated", "Total: 5 marks" in page.inner_text("#totals"), page.inner_text("#totals"))

            # 2. Teacher typing in Q1 while Claude edits Q3: both kept.
            q1 = page.locator("#paper .item .txt").nth(0)
            q1.click()
            page.keyboard.press("End")
            page.keyboard.type(" (teacher)")
            api.claude_edit(uid, lambda e: e["sections"][0]["questions"][2]["blocks"][0].update(value="three, by Claude"))
            page.wait_for_function("document.querySelector('#status').textContent === 'Saved'", timeout=8000)
            time.sleep(2.5)
            saved = texts(api.get(uid)["exam"])
            check("teacher and Claude both kept (different questions)",
                  saved[0] == "one (teacher)" and saved[2] == "three, by Claude", saved)
            page.click("#totals")  # click away from the text, so the paper re-renders
            page.wait_for_timeout(300)
            check("editor shows both", "one (teacher)" in paper() and "three, by Claude" in paper())

            # 3. Both change the same question: the teacher's version wins, and the status says so.
            q1 = page.locator("#paper .item .txt").nth(0)
            q1.click()
            page.keyboard.press("End")
            page.keyboard.type("!")
            api.claude_edit(uid, lambda e: e["sections"][0]["questions"][0]["blocks"][0].update(value="one, by Claude"))
            page.wait_for_function("document.querySelector('#status').textContent.includes('your version was kept')",
                                   timeout=8000)
            page.wait_for_function("document.querySelector('#status').textContent.startsWith('Saved')", timeout=8000)
            check("same question: teacher's version saved", texts(api.get(uid)["exam"])[0] == "one (teacher)!",
                  texts(api.get(uid)["exam"])[0])
            page.click("#totals")
            page.wait_for_timeout(3000)
            check("the 'your version was kept' notice stays after saving", "your version was kept" in status(), status())
            page.locator("#paper .item .txt").nth(0).click()
            page.keyboard.press("End")
            page.keyboard.type("?")
            page.wait_for_function("document.querySelector('#status').textContent === 'Saved'", timeout=8000)
            check("...until the teacher edits again", status() == "Saved", status())
            page.click("#totals")

            # 3b. A save elsewhere that changes nothing visible (only the title) adds no Undo step.
            page.wait_for_timeout(800)
            page.evaluate("undoStack.length = 0; updateUndoButton()")
            current = api.get(uid)
            api.call("PUT", f"exams/{uid}", {**current["exam"], "title": "retitled elsewhere"}, base=current["updated_at"])
            page.wait_for_timeout(3500)
            check("no Undo step for an invisible change", page.is_disabled("#undo-btn"))

            # 4. Undo takes back Claude's change.
            page.wait_for_timeout(1000)
            api.claude_edit(uid, lambda e: e["sections"][0]["questions"].pop())  # Claude removes Q4
            page.wait_for_function("!document.querySelector('#paper').innerText.includes('four, added by Claude')",
                                   timeout=6000)
            page.wait_for_timeout(800)
            page.click("#undo-btn")
            page.wait_for_function("document.querySelector('#status').textContent === 'Saved'", timeout=8000)
            check("Undo brings back what Claude removed", "four, added by Claude" in texts(api.get(uid)["exam"]),
                  texts(api.get(uid)["exam"]))

            # 5. A second tab editing the same exam: the edits merge instead of overwriting each other.
            other = ctx.new_page()
            other.goto(f"{base}/#exam={uid}")
            other.wait_for_selector(".qhead")
            page.locator("#paper .item .txt").nth(1).click()
            page.keyboard.press("End")
            page.keyboard.type(" [tab 1]")
            other.locator("#paper .item .txt").nth(2).click()
            other.keyboard.press("End")
            other.keyboard.type(" [tab 2]")
            for pg in (page, other):
                pg.click("#totals")
            time.sleep(4)
            saved = texts(api.get(uid)["exam"])
            check("two tabs: both edits kept", saved[1].endswith("[tab 1]") and saved[2].endswith("[tab 2]"), saved)
            other.close()

            # 6. An exam from before ids gets them when opened.
            api.call("PUT", "exams/old-exam-0001", {"unit": "Old", "sections": [
                {"name": "A", "questions": [{"marks": 1, "blocks": [{"type": "text", "value": "x"}], "parts": []}]}]})
            page.goto(f"{base}/?reopen#exam=old-exam-0001")  # a real page load, not just a new #
            page.wait_for_selector(".qhead")
            page.wait_for_function("document.querySelector('#status').textContent === 'Saved'", timeout=8000)
            page.wait_for_timeout(1200)
            old = api.get("old-exam-0001")["exam"]
            check("older exam gets ids", bool(old["sections"][0].get("id")) and bool(old["sections"][0]["questions"][0].get("id")))

            # 7. An exam Claude creates appears in My exams without reloading.
            page.click("#back-btn")
            page.wait_for_selector(".row")
            api.call("PUT", "exams/new-from-claude-01", {"unit": "Made by Claude", "sections": [
                {"id": "s", "name": "A", "questions": [question("n1", "x")]}]}, by="claude")
            page.wait_for_function("document.querySelector('#home-list').innerText.includes('Made by Claude')",
                                   timeout=8000)
            check("new exam appears in My exams", True)

            # 8. Pasting the link of the exam last open (now on My exams) opens it.
            page.evaluate(f"location.hash = '#exam=old-exam-0001'")
            page.wait_for_selector("#editor-view:not([hidden]) .qhead", timeout=5000)
            check("a pasted link opens the exam last open", page.is_visible("#editor-view"))
            check("no Use with Claude button without the connector", page.locator("#claude-btn").is_hidden())
            check("no page errors", not errors, errors)
            browser.close()
    finally:
        proc.terminate()

    print("OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
