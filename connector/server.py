"""
Exam Assistant connector: an MCP server that lets Claude write an exam and hand the teacher a link that opens it,
ready to edit and print, in Exam Assistant.

Claude does the writing in the teacher's own Claude account. This server only checks the exam against the editor's
rules and turns it into a link. It holds no staff codes, no school data and no exams beyond the short-lived links.

    pip install -r connector/requirements.txt
    python connector/server.py                    # http://localhost:8000/mcp

Settings (environment variables):
    PUBLIC_URL    where this server is reachable, e.g. https://exam-connector.example.org (used in links)
    DEMO_URL      the editor that opens links (default: the GitHub Pages demo)
    SCHOOL_URL    optional: your school's own Exam Assistant, e.g. http://examserver:7900/ — links then offer
                  "Open on the school server", which saves into the teacher's library
    DATA_DIR      where the short links are kept (default: connector/data)
    LINK_DAYS     how long a link works (default 30)
    PORT          default 8000
    TRUST_PROXY   set to 1 behind a reverse proxy, so rate limits use X-Forwarded-For
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import exam_format  # noqa: E402
from store import LinkStore  # noqa: E402

ROOT = HERE.parent
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
DEMO_URL = os.environ.get("DEMO_URL", "https://fcl-soc.github.io/exam-builder/")
SCHOOL_URL = os.environ.get("SCHOOL_URL", "").strip()
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
LINK_DAYS = float(os.environ.get("LINK_DAYS", "30"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("exam-connector")

GUIDE = (ROOT / "docs" / "exam-format.md").read_text(encoding="utf-8")
EXAMPLE = json.loads((ROOT / "examples" / "sample_exam.json").read_text(encoding="utf-8"))
links = LinkStore(DATA_DIR / "links.db", days=LINK_DAYS)
FORMAT_TEXT = (f"{GUIDE}\n\n## Complete example\n\n```json\n{json.dumps(EXAMPLE, indent=1)}\n```\n\n"
               f"## JSON Schema\n\n```json\n{json.dumps(exam_format.SCHEMA, separators=(',', ':'))}\n```\n")

INSTRUCTIONS = """\
Writes exams, tests and SACs that open in Exam Assistant, a school's A4 exam editor, ready to review and print.

Workflow:
1. Call get_exam_format once before writing an exam. It has the JSON format, the authoring rules and a full example.
2. Write the exam as JSON. Marks go on the deepest parts only. Never type question numbers, part letters or
   marks into text; they are automatic. Money is written \\$12.50. Graph expressions use x and ^.
3. Call check_exam and fix every error. Treat warnings as advice.
4. Call create_exam_link and give the teacher the link. Opening it shows the exam in the editor, where they can
   change anything before printing. Mention that the demo doesn't save: print or save as PDF, or use the school
   server option if the link offers one.
Images can't be included; say where a diagram or photo should go and the teacher can add it in the editor.
"""

mcp = MCPServer(name="exam-assistant", title="Exam Assistant", instructions=INSTRUCTIONS,
                website_url="https://github.com/FCL-SOC/exam-builder", version="1.0.0")

ExamArg = Annotated[dict[str, Any], Field(description="The whole exam as JSON, in the format from get_exam_format.")]


@mcp.tool(title="Get the exam format", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
def get_exam_format() -> str:
    """The exam JSON format: the authoring guide, a complete example exam and the JSON Schema.
    Call this once before writing an exam."""
    return FORMAT_TEXT


@mcp.tool(title="Check an exam", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
def check_exam(exam: ExamArg) -> dict[str, Any]:
    """Check an exam against the editor's rules before making a link. Returns ok, errors (must fix),
    warnings (advice), total_marks and the number of questions."""
    return exam_format.validate(exam)


@mcp.tool(title="Create a link to the exam",
          annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                      open_world_hint=False))
def create_exam_link(exam: ExamArg) -> dict[str, Any]:
    """Check the exam and, if it has no errors, return a link that opens it in Exam Assistant for the teacher to
    review, edit and print. Give the teacher the link exactly as returned."""
    result = exam_format.validate(exam)
    if not result["ok"]:
        return {**result, "link": None, "message": "Fix these errors, then call create_exam_link again."}
    normalised = exam_format.normalise(exam, result["total_marks"])
    summary = {"title": normalised["title"], "subject": exam.get("subject", ""), "unit": exam.get("unit", ""),
               "assessment_type": exam.get("assessment_type", ""), "year_level": exam.get("year_level", ""),
               "total_marks": result["total_marks"], "questions": result["questions"],
               "sections": len(exam["sections"])}
    link_id, expires = links.put(exam_format.pack(normalised), summary)
    log.info("link %s: %s marks, %s questions", link_id, result["total_marks"], result["questions"])
    return {"ok": True, "link": f"{PUBLIC_URL}/e/{link_id}", "expires": _date(expires),
            "title": normalised["title"], "total_marks": result["total_marks"], "questions": result["questions"],
            "warnings": result["warnings"]}


@mcp.prompt(title="Write an exam")
def write_exam(subject: str, year_level: str, topic: str, assessment_type: str = "Test",
               total_marks: str = "40", writing_minutes: str = "50", notes: str = "") -> str:
    """Draft an exam, test or SAC and open it in Exam Assistant."""
    return (f"Write a {assessment_type} for Year {year_level} {subject} on {topic}: about {total_marks} marks, "
            f"{writing_minutes} minutes of writing time. Use a multiple-choice section followed by short and "
            "extended answer questions unless the notes say otherwise, with a range of difficulty and answer space "
            "that suits each question's marks. Mark the correct option on every multiple-choice question."
            + (f"\n\nNotes from the teacher: {notes}" if notes else "")
            + "\n\nUse the Exam Assistant tools: get the format, check the exam, then give me the link.")


# ------------------------------------------------------------------ links
def _date(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return f"{dt.day} {dt:%B %Y}"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{title}</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; background: #f4f5f7; color: #1d2330; margin: 0; padding: 24px 16px; }}
  main {{ max-width: 560px; margin: 8vh auto 0; background: #fff; border-radius: 10px; padding: 28px;
          box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }} p {{ margin: 8px 0; }} .meta {{ color: #5b6475; }}
  a.btn {{ display: block; text-align: center; padding: 12px; border-radius: 8px; margin: 14px 0 0;
           text-decoration: none; font-weight: 600; background: #1f4e8c; color: #fff; }}
  a.btn.alt {{ background: #fff; color: #1f4e8c; border: 1px solid #1f4e8c; }}
  .note {{ font-size: 14px; color: #5b6475; margin-top: 18px; }}
</style></head><body><main>{body}</main></body></html>"""


@mcp.custom_route("/e/{link_id}", methods=["GET"], include_in_schema=False)
async def open_link(request: Request) -> Response:
    found = links.get(request.path_params["link_id"])
    if not found:
        body = ("<h1>This exam link has expired</h1><p>Links last "
                f"{LINK_DAYS:g} days. Ask Claude to make the exam link again.</p>")
        return HTMLResponse(PAGE.format(title="Link expired", body=body), status_code=404)
    packed, s, expires = found
    e = html.escape
    details = " · ".join(str(x) for x in [s.get("assessment_type"), s.get("year_level") and f"Year {s['year_level']}",
                                             f"{s['total_marks']} marks", f"{s['questions']} questions"] if x)
    buttons = []
    if SCHOOL_URL:
        buttons.append(f'<a class="btn" href="{e(SCHOOL_URL)}#data={packed}">Open on the school server</a>'
                       '<p class="note">Saves it to your exams under your staff code. Works on the school network.</p>')
    demo_class = "btn alt" if SCHOOL_URL else "btn"
    buttons.append(f'<a class="{demo_class}" href="{e(DEMO_URL)}#data={packed}">Open in the online editor</a>'
                   '<p class="note">Nothing is saved there: print it or save it as a PDF before you close the tab.</p>')
    body = (f"<h1>{e(s.get('title') or 'Exam')}</h1><p class='meta'>{e(details)}</p>{''.join(buttons)}"
            f"<p class='note'>This link works until {_date(expires)}.</p>")
    return HTMLResponse(PAGE.format(title=e(s.get("title") or "Exam"), body=body),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "links": links.count()})


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(_: Request) -> Response:
    body = ("<h1>Exam Assistant connector</h1><p>This is an MCP server. Add it to Claude as a custom connector "
            f"using <code>{html.escape(PUBLIC_URL)}/mcp</code>.</p>"
            '<p class="note"><a href="https://github.com/FCL-SOC/exam-builder">About Exam Assistant</a></p>')
    return HTMLResponse(PAGE.format(title="Exam Assistant connector", body=body))


# ------------------------------------------------------------------ app
class RateLimit:
    """Per-client cap on /mcp calls: an open, unauthenticated endpoint shouldn't be usable as free storage."""

    def __init__(self, app, limit: int = 120, window: float = 600) -> None:
        self.app, self.limit, self.window = app, limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp") and scope["method"] == "POST":
            client = (scope.get("client") or ("?",))[0]
            if TRUST_PROXY:
                fwd = dict(scope["headers"]).get(b"x-forwarded-for", b"").decode().split(",")[0].strip()
                client = fwd or client
            now, q = time.monotonic(), self.hits[client]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                await Response("Too many requests. Try again in a few minutes.", status_code=429)(scope, receive, send)
                return
            q.append(now)
            if len(self.hits) > 10_000:  # forget idle clients
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                    del self.hits[k]
        await self.app(scope, receive, send)


def build_app():
    public = urlsplit(PUBLIC_URL)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc, "localhost:*", "127.0.0.1:*", "localhost", "127.0.0.1"],
        allowed_origins=[f"{public.scheme}://{public.netloc}", "https://claude.ai", "https://claude.com",
                         "http://localhost:*", "http://127.0.0.1:*"],
    )
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security,
                                  max_request_body_size=1_000_000, host="0.0.0.0")
    return RateLimit(app)


app = build_app()

if __name__ == "__main__":
    log.info("MCP endpoint %s/mcp; links open %s%s", PUBLIC_URL, DEMO_URL, f" or {SCHOOL_URL}" if SCHOOL_URL else "")
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=TRUST_PROXY, forwarded_allow_ips="*" if TRUST_PROXY else None)
