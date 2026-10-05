"""
The Claude Desktop extension's proxy (desktop-extension/server/index.js), driven the way Claude Desktop drives it:
an MCP client over stdio → the proxy → the connector over HTTP (school mode) → a real Exam Assistant.

Run: python -m unittest discover connector/tests   (skipped when Node.js isn't installed)
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path

from mcp import Client, StdioServerParameters

CONNECTOR = Path(__file__).resolve().parent.parent
ROOT = CONNECTOR.parent
PROXY = CONNECTOR / "desktop-extension" / "server" / "index.js"
SAMPLE = json.loads((ROOT / "examples" / "sample_exam.json").read_text(encoding="utf-8"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url):
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} didn't start")


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class DesktopExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        for name in ("server.py", "importer.py"):
            shutil.copy(ROOT / name, cls.tmp)
        shutil.copytree(ROOT / "static", cls.tmp / "static")
        app_port, connector_port = free_port(), free_port()
        cls.app = f"http://127.0.0.1:{app_port}"
        cls.connector = f"http://127.0.0.1:{connector_port}"
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        cls.procs = [subprocess.Popen([sys.executable, str(cls.tmp / "server.py"), str(app_port)], cwd=cls.tmp, **quiet)]
        wait_for(cls.app + "/api/settings")
        env = {**os.environ, "EXAM_SERVER": cls.app, "EDITOR_URL": "http://8801-openai-01:7900/",
               "PORT": str(connector_port), "PUBLIC_URL": cls.connector, "DATA_DIR": str(cls.tmp / "connector")}
        env.pop("STAFF_CODE", None)
        cls.procs.append(subprocess.Popen([sys.executable, str(CONNECTOR / "server.py")], env=env, **quiet))
        wait_for(cls.connector + "/healthz")

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.terminate()
            p.wait()

    def run_client(self, staff_code="abc", url=None, steps=None):
        params = StdioServerParameters(command="node", args=[str(PROXY)], env={
            **os.environ, "EXAM_CONNECTOR_URL": url or self.connector, "STAFF_CODE": staff_code})

        async def go():
            async with Client(params) as c:
                return await steps(c)
        return asyncio.run(go())

    def test_tools_and_a_whole_exam(self):
        async def steps(c):
            tools = [t.name for t in (await c.list_tools()).tools]
            created = await c.call_tool("create_exam", {"exam": SAMPLE})
            listed = await c.call_tool("list_my_work", {})
            return tools, created.structured_content, listed.structured_content
        tools, created, listed = self.run_client(steps=steps)
        self.assertEqual(tools, ["get_format", "list_my_work", "read", "create_exam", "edit_exam", "write_lesson_plan",
                                 "lesson_plan_preview", "undo"])
        self.assertTrue(created["ok"], created)
        self.assertEqual(listed["staff_code"], "ABC")  # the code from the install screen, upper-cased
        with urllib.request.urlopen(f"{self.app}/api/exams/{created['exam_id']}?owner=ABC") as r:
            saved = json.loads(r.read())
        self.assertEqual((saved["owner"], saved["updated_by"]), ("ABC", "claude"))

    def test_edits_go_through(self):
        async def steps(c):
            created = (await c.call_tool("create_exam", {"exam": SAMPLE})).structured_content
            read = (await c.call_tool("read", {"item_id": created["exam_id"]})).structured_content
            section = read["exam"]["sections"][0]["id"]
            edited = await c.call_tool("edit_exam", {"exam_id": created["exam_id"], "changes": [
                {"op": "add", "to": section, "items": [{"marks": 1, "blocks": [
                    {"type": "text", "value": "Added through the extension"},
                    {"type": "choices", "options": ["yes", "no"], "correct": 0}]}]}]})
            return edited.structured_content
        edited = self.run_client(steps=steps)
        self.assertTrue(edited["ok"], edited)
        self.assertIn("Added through the extension", edited["outline"])

    def test_without_a_staff_code_the_teacher_is_told_how_to_fix_it(self):
        async def steps(c):
            r = await c.call_tool("list_my_work", {})
            return r.is_error, r.content[0].text
        is_error, text = self.run_client(staff_code="", steps=steps)
        self.assertTrue(is_error)
        self.assertIn("click Use with Claude", text)

    def test_off_the_school_network(self):
        """Nothing listening: the proxy answers with a message that says what to do."""
        proc = subprocess.run(["node", str(PROXY)], input=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}) + "\n",
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "EXAM_CONNECTOR_URL": f"http://127.0.0.1:{free_port()}", "STAFF_CODE": "ABC"})
        reply = json.loads(proc.stdout.strip())
        self.assertEqual(reply["id"], 1)
        self.assertIn("only works on the school network", reply["error"]["message"])


class BundleTests(unittest.TestCase):
    def test_the_packed_bundle_matches_its_source(self):
        """exam-assistant.mcpb is what teachers install: rebuild it after changing the extension
        (npx @anthropic-ai/mcpb pack connector/desktop-extension connector/desktop-extension/exam-assistant.mcpb)."""
        folder = CONNECTOR / "desktop-extension"
        with zipfile.ZipFile(folder / "exam-assistant.mcpb") as bundle:
            for name in ("manifest.json", "server/index.js", "icon.png"):
                self.assertEqual(bundle.read(name), (folder / name).read_bytes(), f"{name} is out of date in the bundle")


if __name__ == "__main__":
    unittest.main()
