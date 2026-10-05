"""
The lesson plan page in a real browser: writing a plan, how it renders, and changes from Claude arriving live and
merging with the teacher's own. Needs Playwright and Chromium, like browser_check.py:

    python tests/plans_check.py

"Claude" here is the same version-checked API call the connector makes. Exits non-zero on any failure.
"""

import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import free_port  # noqa: E402
from live_check import Api  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def claude_edit(api, uid, change):
    for _ in range(5):
        current = api.call("GET", f"plans/{uid}")[1]
        plan = current["exam"]
        change(plan)
        if api.call("PUT", f"plans/{uid}", plan, base=current["updated_at"], by="claude")[0] == 200:
            return
    raise RuntimeError("kept clashing")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(ROOT / "server.py", tmp)
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
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context()
            ctx.add_init_script("localStorage.setItem('staffCode', 'ABC')")
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            # From the exam app to lesson plans, then a new plan.
            page.goto(base + "/")
            page.click("#plans-btn")
            page.wait_for_selector("#new-btn:not([disabled])")
            page.click("#new-btn")
            page.fill('[data-field="class_code"]', "10MM1")
            page.fill('[data-field="topic"]', "Completing the square")
            page.click('.content[data-k="L"]')
            page.keyboard.type("**Learning Intentions**\nTo complete the square for $x^2 + 6x$\n- I can expand $(x+3)^2$\n- I can find the turning point")
            page.click("h1")  # click away: the section renders
            page.wait_for_function("document.querySelector('#status').textContent === 'Saved'", timeout=8000)
            cell = page.locator('.content[data-k="L"]')
            check("bold renders", cell.locator("b").inner_text() == "Learning Intentions")
            check("bullets render", cell.locator("li").count() == 2)
            check("maths renders", cell.locator(".ML__latex").count() >= 2)
            uid = page.evaluate("uid")
            saved = api.call("GET", f"plans/{uid}")[1]
            check("saved with its details", (saved["exam"]["class_code"], saved["exam"]["title"]) == ("10MM1", "10MM1 · Completing the square"),
                  saved["exam"].get("title"))

            # Claude writes another section: it appears live and flashes.
            started = time.time()
            claude_edit(api, uid, lambda p: p["sections"].update(E="**Completing the square**\nWrite $x^2+bx$ as $(x+\\frac{b}{2})^2-(\\frac{b}{2})^2$."))
            page.wait_for_function("document.querySelector('.content[data-k=\"E\"]').innerText.includes('Completing the square')", timeout=6000)
            check(f"Claude's section appears within 3 s ({time.time() - started:.1f} s)", time.time() - started < 3.5)
            check("it flashes", page.locator('tr[data-k="E"].remote-change').count() == 1)
            check("status says Claude", page.inner_text("#status") == "Updated by Claude", page.inner_text("#status"))

            # Teacher is changing L while Claude changes L and A: teacher keeps L, Claude's A arrives.
            page.click('.content[data-k="L"]')
            page.keyboard.press("Control+End")
            page.keyboard.type("\n- I can solve by completing the square")
            claude_edit(api, uid, lambda p: p["sections"].update(L="Claude's version", A="Complete Exercise 4F Q1–8."))
            page.click("h1")
            page.wait_for_function("document.querySelector('#status').textContent.startsWith('Saved')", timeout=10000)
            time.sleep(2.5)
            final = api.call("GET", f"plans/{uid}")[1]["exam"]["sections"]
            check("same section: the teacher's version kept", final["L"].endswith("I can solve by completing the square"), final["L"][-60:])
            check("different section: Claude's kept", final["A"] == "Complete Exercise 4F Q1–8.", final["A"])
            check("the teacher is told", "your version was kept" in page.inner_text("#status"), page.inner_text("#status"))

            # Undo, the list, and the link Claude would give.
            page.click("#back-btn")
            page.wait_for_selector(".row")
            check("My lesson plans lists it", "10MM1 · Completing the square" in page.inner_text("#home-list"))
            page.goto(f"{base}/plans.html?again#plan={uid}")
            page.wait_for_selector('.content[data-k="A"]')
            check("a #plan= link opens it", "Exercise 4F" in page.inner_text("#learn"))
            check("no page errors", not errors, errors)
            browser.close()
    finally:
        proc.terminate()
    print("OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
