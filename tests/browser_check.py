"""
Open a #data= link in a real browser and check the exam arrives intact.

Not part of the default pytest run (it needs Playwright and Chromium):

    pip install playwright && python -m playwright install chromium
    python tests/browser_check.py            # demo mode, served from static/
    python tests/browser_check.py --server   # the real server, checks the exam is saved to SQLite

Exits non-zero on any failure.
"""

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connector"))
import exam_format  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    use_server = "--server" in sys.argv
    exam = json.loads((ROOT / "examples" / "sample_exam.json").read_text(encoding="utf-8"))
    result = exam_format.validate(exam)
    assert result["ok"], result
    packed = exam_format.pack(exam_format.normalise(exam, result["total_marks"]))
    port = free_port()
    proc = httpd = None
    data_dir = Path(tempfile.mkdtemp())
    if use_server:
        # A throwaway copy, because the server keeps its database next to server.py.
        shutil.copy(ROOT / "server.py", data_dir)
        shutil.copytree(ROOT / "static", data_dir / "static")
        proc = subprocess.Popen([sys.executable, str(data_dir / "server.py"), str(port)], cwd=data_dir,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        url = f"http://127.0.0.1:{port}/#data={packed}"
    else:
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Quiet, directory=str(ROOT / "static")))
        Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/index.html?demo#data={packed}"

    failures = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: (errors.append("dialog: " + d.message), d.dismiss()))
            page.goto(url)
            page.wait_for_selector("#home-list p")
            waiting = page.inner_text("#home-list")
            if "open the exam from your link" not in waiting:
                failures.append(f"no staff-code prompt for the link: {waiting!r}")
            page.fill("#code", "ABC")
            page.wait_for_selector("#editor-view:not([hidden]) .qhead")
            page.wait_for_function("document.querySelector('#status').textContent === 'Saved'", timeout=10_000)

            checks = {
                "questions": page.locator(".qhead").count(),
                "totals": page.inner_text("#totals"),
                "hash": page.evaluate("location.hash"),
                "graphs": page.locator("#paper .graph svg").count(),
                "curves": page.locator("#paper path.g-fn").count(),
                "boxplots": page.locator("#paper .g-box").count(),
                "mc_options": page.locator("#paper .opt").count(),
                "correct_marked": page.locator("#paper .mcl.is-correct").count(),
                "answer_boxes": page.locator("#paper .abx").count(),
                "text": page.inner_text("#paper"),
                "print_enabled": page.is_enabled("#print-btn"),
                "maths": page.locator("#paper .ML__latex").count(),
            }
            page.screenshot(path=str(data_dir / "imported.png"), full_page=True)
            browser.close()
    finally:
        if httpd:
            httpd.shutdown()
        if proc:
            proc.terminate()

    expect = {"questions": 7, "graphs": 7, "curves": 4, "boxplots": 1, "mc_options": 12, "correct_marked": 3,
              "answer_boxes": 6}
    for key, want in expect.items():
        if checks[key] != want:
            failures.append(f"{key}: expected {want}, got {checks[key]}")
    if "Total: 20 marks" not in checks["totals"]:
        failures.append(f"totals: {checks['totals']!r}")
    if not checks["hash"].startswith("#exam="):
        failures.append(f"hash not swapped to #exam=: {checks['hash'][:40]!r}")
    for needle in ("costs $80 after", "$100", "Section A: Multiple choice", "Functions and Statistics"):
        if needle not in checks["text"]:
            failures.append(f"missing text {needle!r}")
    if "\\$" in checks["text"]:
        failures.append("a backslash-dollar was printed literally")
    if not checks["print_enabled"]:
        failures.append("Print is disabled (some item has no marks)")
    if checks["maths"] < 10:
        failures.append(f"only {checks['maths']} maths elements rendered")
    if errors:
        failures.append(f"page errors: {errors}")
    if use_server:
        db = data_dir / "data" / "exams.db"
        rows = sqlite3.connect(db).execute("SELECT owner, body FROM exams").fetchall() if db.exists() else []
        saved = [json.loads(b) for o, b in rows if o == "ABC" and "Functions and Statistics" in b]
        if not saved:
            failures.append(f"exam not saved to {db}")
        elif saved[-1].get("total_marks") != 20:
            failures.append(f"saved total_marks {saved[-1].get('total_marks')}")

    print(json.dumps({k: v for k, v in checks.items() if k != "text"}, indent=1))
    print("screenshot:", data_dir / "imported.png")
    for f in failures:
        print("FAIL", f)
    print("OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
