"""
The lesson plan preview Claude shows in the chat (connector/lesson-plan-view.html), in a real browser, driven by
the reference MCP Apps host (AppBridge from @modelcontextprotocol/ext-apps) the way Claude Desktop drives it: the
page is loaded into a sandboxed frame, given write_lesson_plan's result, and its own tool calls go to the real
connector. Needs Node.js (to build the reference host once) and Playwright:

    python tests/plan_view_check.py

Exits non-zero on any failure.
"""

import json
import os
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

HOST_VERSION = "1.7.5"
HOST_JS = """
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";
// As a real host does: listen first, then load the page into the frame, then hand it the tool result.
window.startHost = async (iframe, html, toolResult) => {
  const bridge = new AppBridge(null, { name: "TestHost", version: "1.0.0" }, { openLinks: {}, serverTools: {} },
                               { hostContext: { theme: "light", displayMode: "inline" } });
  window.opened = []; window.sizes = []; window.toolCalls = [];
  bridge.onopenlink = async ({ url }) => { window.opened.push(url); return {}; };
  bridge.onsizechange = p => window.sizes.push(p);
  bridge.oncalltool = async params => { window.toolCalls.push(params.name); return await window.callTool(params); };
  const ready = new Promise(r => { bridge.oninitialized = r; });
  await bridge.connect(new PostMessageTransport(iframe.contentWindow, iframe.contentWindow));
  iframe.srcdoc = html;
  await ready;
  await bridge.sendToolResult(toolResult);
};
"""

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def build_host(tmp: Path) -> Path:
    """The reference host, bundled for the browser (npm and esbuild, once per run)."""
    work = tmp / "host"
    work.mkdir()
    (work / "host.js").write_text(HOST_JS)
    npm = shutil.which("npm") or "npm"
    subprocess.run([npm, "init", "-y"], cwd=work, check=True, capture_output=True)
    subprocess.run([npm, "install", "--silent", f"@modelcontextprotocol/ext-apps@{HOST_VERSION}",
                    "@modelcontextprotocol/sdk@^1.29", "zod@^4", "esbuild"], cwd=work, check=True, capture_output=True)
    subprocess.run([shutil.which("npx") or "npx", "esbuild", "host.js", "--bundle", "--format=esm",
                    "--outfile=host.bundle.js", "--log-level=error"], cwd=work, check=True)
    return work / "host.bundle.js"


def mcp(url, method, params):
    """One JSON-RPC call to the connector, as the teacher's extension makes it."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Staff-Code": "ABC",
                                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=20) as r:
        reply = json.loads(r.read())
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["result"]


def wait_for(url):
    for _ in range(100):
        try:
            urllib.request.urlopen(url).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} didn't start")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    host_bundle = build_host(tmp)
    for name in ("server.py", "importer.py"):
        shutil.copy(ROOT / name, tmp)
    shutil.copytree(ROOT / "static", tmp / "static")
    app_port, conn_port = free_port(), free_port()
    app, conn = f"http://127.0.0.1:{app_port}", f"http://127.0.0.1:{conn_port}"
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    procs = [subprocess.Popen([sys.executable, str(tmp / "server.py"), str(app_port)], cwd=tmp, **quiet)]
    try:
        wait_for(app + "/api/settings")
        env = {**os.environ, "EXAM_SERVER": app, "EDITOR_URL": "http://8801-openai-01:7900/", "PORT": str(conn_port),
               "PUBLIC_URL": conn, "DATA_DIR": str(tmp / "connector")}
        env.pop("STAFF_CODE", None)
        procs.append(subprocess.Popen([sys.executable, str(ROOT / "connector" / "server.py")], env=env, **quiet))
        wait_for(conn + "/healthz")
        endpoint = conn + "/mcp"

        # Claude writes a plan; this is the result Claude Desktop hands to the preview.
        result = mcp(endpoint, "tools/call", {"name": "write_lesson_plan", "arguments": {
            "details": {"class_code": "10MM1", "topic": "Completing the square", "lesson_date": "2026-10-14"},
            "sections": {"L": "**Learning Intentions**\nTo complete the square.\n**Success Criteria**\nI can expand $(x+3)^2$",
                         "E": "**Example**\n$$\\frac{b}{2}$$\nTickets cost \\$12.50."}}})
        uid = result["structuredContent"]["plan_id"]
        page_html = mcp(endpoint, "resources/read", {"uri": "ui://exam-assistant/lesson-plan"})["contents"][0]["text"]
        teacher = Api(app, "ABC")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.expose_function("callToolPy", lambda params: mcp(endpoint, "tools/call", params))
            page.set_content("<!doctype html><body></body>")
            page.add_script_tag(content=host_bundle.read_text(), type="module")
            page.wait_for_function("typeof window.startHost === 'function'")
            # A sandboxed frame without same-origin, as Claude's is.
            page.evaluate("""() => { window.callTool = p => window.callToolPy(p);
                const f = document.createElement('iframe'); f.id = 'view'; f.sandbox = 'allow-scripts';
                f.style.width = '760px'; f.style.height = '700px'; document.body.append(f); }""")
            page.evaluate("([html, r]) => Promise.race([window.startHost(document.getElementById('view'), html, r), "
                          "new Promise((_, no) => setTimeout(() => no(new Error('the preview never said hello')), 10000))])",
                          [page_html, result])
            view = page.frames[1]
            view.wait_for_function("document.querySelectorAll('#rows tr').length === 5", timeout=5000)

            text = view.inner_text("body").replace("\u00a0", " ")
            check("the preview shows the plan's title", "10MM1 · Completing the square" in text, text[:200])
            check("all five LEARN rows", view.locator("#rows tr").count() == 5)
            check("maths as it pastes into Compass", "(x+3)²" in text, text)
            check("a fraction written out", "b/2" in text, text)
            check("money", "$12.50" in text)
            check("L lines are dot points, as in Compass", "• I can expand" in text, text)
            check("empty sections say so", "Not written yet" in text)
            check("it tells Claude its height", any((s.get("height") or 0) > 200 for s in page.evaluate("window.sizes")),
                  page.evaluate("window.sizes"))

            # A change made elsewhere (the teacher in Exam Assistant) appears by itself, and flashes.
            current = teacher.call("GET", f"plans/{uid}")[1]
            current["exam"]["sections"]["A"] = "Complete Exercise 4F Q1–8."
            teacher.call("PUT", f"plans/{uid}", current["exam"], base=current["updated_at"])
            started = time.time()
            view.wait_for_function("document.body.innerText.includes('Exercise 4F')", timeout=8000)
            check(f"a change appears by itself ({time.time() - started:.1f} s)", time.time() - started < 5)
            check("the changed section flashes", view.locator('tr[data-k="A"].changed').count() == 1)
            check("only the changed section flashes", view.locator("tr.changed").count() == 1)
            check("refreshes go through the preview's own tool",
                  set(page.evaluate("window.toolCalls")) == {"lesson_plan_preview"}, page.evaluate("window.toolCalls"))

            view.click("#open")
            page.wait_for_function("window.opened.length === 1")
            check("Open in Exam Assistant opens the plan", page.evaluate("window.opened")[0] ==
                  f"http://8801-openai-01:7900/plans.html#plan={uid}", page.evaluate("window.opened"))
            check("no page errors", not errors, errors)
            if os.environ.get("KEEP_OUTPUT"):
                page.locator("#view").screenshot(path=os.environ["KEEP_OUTPUT"])
            browser.close()
    finally:
        for proc in procs:
            proc.terminate()
    print("OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
