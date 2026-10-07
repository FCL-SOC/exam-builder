"""
Open a #data= link in a real browser and check the exam arrives intact.

Not part of the default pytest run (it needs Playwright and Chromium):

    pip install playwright && python -m playwright install chromium
    python tests/browser_check.py            # demo mode, served from static/
    python tests/browser_check.py --server   # the real server, checks the exam is saved to SQLite

Exits non-zero on any failure.
"""

import base64
import copy
import json
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import zlib
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


def png(w: int, h: int) -> str:
    """A plain w x h picture as a data URI."""
    chunk = lambda kind, data: (len(data).to_bytes(4, "big") + kind + data  # noqa: E731
                                + zlib.crc32(kind + data).to_bytes(4, "big"))
    rows = b"".join(b"\x00" + b"\x80\x80\xc0" * w for _ in range(h))
    ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def mixed_exam(sample: dict, seed: int = 2) -> dict:
    """Three sections of questions and parts built at random from the sample's blocks plus a picture, an equation
    and long maths text: tall parts, parts that only just fit a page, and a last question that nearly fills one,
    which is where the preview's page breaks and Chrome's used to disagree."""
    blocks = []

    def collect(it):
        blocks.extend(it["blocks"])
        for p in it.get("parts", []):
            collect(p)
    for s in sample["sections"]:
        for q in s["questions"]:
            collect(q)
    blocks.append({"type": "image", "value": png(400, 260), "width": 60})
    blocks.append({"type": "equation", "value": "\\frac{1}{2}mv^2 = mgh"})
    blocks.append({"type": "text", "value": "Given that $\\frac{dy}{dx} = 3x^2 - 4$ and $y(1) = \\sqrt{2}$, find $y$ when "
                   "$x = \\frac{3}{2}$. Show all working and give your answer correct to two decimal places."})
    leaf = [b for b in blocks if b["type"] != "choices"]
    mc = [next(b for b in blocks if b["type"] == "text"), next(b for b in blocks if b["type"] == "choices")]
    rnd = random.Random(seed)

    def item(depth):
        it = {"blocks": [copy.deepcopy(rnd.choice(leaf)) for _ in range(rnd.randint(1, 3))], "parts": []}
        if depth < 2 and rnd.random() < 0.5:
            it["parts"] = [item(depth + 1) for _ in range(rnd.randint(2, 3))]
        else:
            it["marks"] = rnd.randint(1, 4)
        return it
    sections = []
    for si in range(3):
        qs = [item(0) for _ in range(rnd.randint(3, 6))]
        if si == 0:
            qs = [{"marks": 1, "blocks": copy.deepcopy(mc), "parts": []} for _ in range(4)] + qs
        sections.append({"name": "ABC"[si], "description": "Mixed", "instructions": "Answer all questions.", "questions": qs})
    return {**{k: v for k, v in sample.items() if k != "sections"}, "unit": f"Mixed check {seed}", "sections": sections}


def print_matches_preview(browser, url: str) -> str | None:
    """Open an exam and print it: Chrome must break pages exactly where the preview shows them (no blank pages,
    nothing pushed on). None if it does, else what differs."""
    page = browser.new_page()  # a new #data= in the old tab would only be a hash change, not a fresh load
    try:
        page.goto(url)
        page.fill("#code", "ABC")
        page.wait_for_selector("#editor-view:not([hidden]) .qhead")
        page.wait_for_timeout(3000)  # maths, pictures and the 250 ms repaginate settle
        shown = page.locator("#paper .band").count() + 2 + page.locator("#paper .sheet.extra").count()  # + cover
        printed = len(re.findall(rb"/Type\s*/Page[^s]", page.pdf(prefer_css_page_size=True)))
        questions = page.locator("#paper .qhead").count()
    finally:
        page.close()
    return None if printed == shown else f"{questions} questions: preview shows {shown} pages but it prints on {printed}"


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
        for name in ("server.py", "importer.py"):
            shutil.copy(ROOT / name, data_dir)
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
            for seed in range(1, 6):
                mixed = mixed_exam(exam, seed)
                mixed_result = exam_format.validate(mixed)
                assert mixed_result["ok"], mixed_result["errors"]
                mixed_packed = exam_format.pack(exam_format.normalise(mixed, mixed_result["total_marks"]))
                problem = print_matches_preview(browser, url.split("#")[0] + "#data=" + mixed_packed)
                print(f"mixed exam {seed}: {problem or 'prints as previewed'}")
                if problem:
                    failures.append(f"mixed exam {seed}: {problem}")
            browser.close()
    finally:
        if httpd:
            httpd.shutdown()
        if proc:
            proc.terminate()

    expect = {"questions": 7, "graphs": 7, "curves": 4, "boxplots": 1, "mc_options": 12, "correct_marked": 3,
              "answer_boxes": 0}
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
