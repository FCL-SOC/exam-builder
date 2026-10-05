"""
School mode end to end: the connector creates and edits an exam while a teacher has it open in Chromium.

Starts a real Exam Assistant and the connector in school mode, then talks to the connector as Claude does
(MCP over HTTP with the staff code header, as the desktop extension sends it). Needs Playwright and Chromium:

    python tests/school_check.py

Exits non-zero on any failure.
"""

import asyncio
import json
import zipfile
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import free_port  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def wait_for(url):
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} didn't start")


def claude(connector, tool, **args):
    """One tool call, as Claude Desktop makes it through the extension."""
    async def go():
        transport = streamable_http_client(f"{connector}/mcp", http_client=create_mcp_http_client(headers={"X-Staff-Code": "ABC"}))
        async with Client(transport) as c:
            r = await c.call_tool(tool, args)
            if r.is_error:
                raise RuntimeError(r.content[0].text)
            return r.structured_content
    with ThreadPoolExecutor(1) as pool:  # Playwright's sync API holds this thread's event loop
        return pool.submit(asyncio.run, go()).result()


def q(text, marks=1):
    return {"marks": marks, "blocks": [{"type": "text", "value": text}, {"type": "lines", "n": 3}]}


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(ROOT / "server.py", tmp)
    shutil.copytree(ROOT / "static", tmp / "static")
    shutil.copytree(ROOT / "connector" / "desktop-extension", tmp / "connector" / "desktop-extension")
    app_port, connector_port = free_port(), free_port()
    app, connector = f"http://127.0.0.1:{app_port}", f"http://127.0.0.1:{connector_port}"
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    procs = [subprocess.Popen([sys.executable, str(tmp / "server.py"), str(app_port)], cwd=tmp, **quiet,
                              env={**os.environ, "CONNECTOR_PORT": str(connector_port)})]
    procs.append(subprocess.Popen([sys.executable, str(ROOT / "connector" / "server.py")], **quiet, env={
        **os.environ, "EXAM_SERVER": app, "EDITOR_URL": app + "/", "PORT": str(connector_port),
        "PUBLIC_URL": connector, "DATA_DIR": str(tmp / "connector")}))
    try:
        wait_for(app + "/api/settings")
        wait_for(connector + "/healthz")

        # Claude writes a short test in one go.
        created = claude(connector, "create_exam", exam={
            "unit": "Quadratics", "subject": "Year 10 Mathematics", "assessment_type": "Test", "year_level": "10",
            "sections": [{"name": "A", "description": "Short answer",
                          "questions": [q("Expand $(x+2)(x-3)$.", 2), q("Factorise $x^2 - 9$.", 2)]}]})
        check("create_exam saves it", created["ok"], created)
        link = created["editor_link"]

        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context()
            ctx.add_init_script("localStorage.setItem('staffCode', 'ABC')")
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # Installing: the button appears because the connector is running; the download is filled in.
            page.goto(app + "/")
            page.wait_for_selector("#claude-btn:not([hidden])", timeout=5000)
            page.click("#claude-btn")
            check("the install dialog shows the teacher's code", page.inner_text("#claude-code") == "ABC")
            with page.expect_download() as download:
                page.click("#claude-download")
            bundle = zipfile.ZipFile(download.value.path())
            config = json.loads(bundle.read("manifest.json"))["user_config"]
            check("the extension is filled in", (config["staff_code"]["default"], config["server_url"]["default"])
                  == ("ABC", f"http://127.0.0.1:{connector_port}"), config)
            check("downloaded as exam-assistant.mcpb", download.value.suggested_filename == "exam-assistant.mcpb")
            page.click("#claude button.primary")

            page.goto(link)  # the link Claude gives the teacher
            page.wait_for_selector(".qhead")
            check("the editor link opens the exam", "Factorise" in page.inner_text("#paper"))

            # The teacher asks for one more question and a change to question 1; they watch it happen.
            read = claude(connector, "get_exam", exam_id=created["exam_id"])
            section = read["exam"]["sections"][0]["id"]
            first = read["exam"]["sections"][0]["questions"][0]["id"]
            started = time.time()
            edited = claude(connector, "edit_exam", exam_id=created["exam_id"], changes=[
                {"op": "add", "to": section, "items": [q("Solve $x^2 - 5x + 6 = 0$.", 3)]},
                {"op": "replace", "id": first, "item": q("Expand and simplify $(x+2)(x-3)$.", 2)}])
            check("edit_exam saves", edited["ok"], edited)
            page.wait_for_function("document.querySelector('#paper').innerText.includes('Solve')", timeout=6000)
            check(f"the teacher sees it within 3 s ({time.time() - started:.1f} s)", time.time() - started < 3.5)
            check("the rewritten question shows", "Expand and simplify" in page.inner_text("#paper"))
            check("totals follow", "Total: 7 marks" in page.inner_text("#totals"), page.inner_text("#totals"))
            check("status says Claude", page.inner_text("#status") == "Updated by Claude", page.inner_text("#status"))

            # Undo from the chat.
            claude(connector, "restore_version", exam_id=created["exam_id"])
            page.wait_for_function("!document.querySelector('#paper').innerText.includes('Solve')", timeout=6000)
            check("restore_version shows live too", "Expand and simplify" not in page.inner_text("#paper"))
            check("no page errors", not errors, errors)
            browser.close()
    finally:
        for proc in procs:
            proc.terminate()

    print("OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
