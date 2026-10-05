"""
Exam Assistant connector: an MCP server that lets Claude write and edit exams in Exam Assistant.

Two ways to run it:

School mode (EXAM_SERVER is set) — next to the school's Exam Assistant, on the school network. Claude creates and
edits exams in the teacher's own library; the teacher watches the changes appear live in the editor. Teachers
connect through the Claude Desktop extension in connector/desktop-extension/, which sends their staff code.

    EXAM_SERVER=http://127.0.0.1:7900 EDITOR_URL=http://8801-openai-01:7900/ PORT=7901 python connector/server.py

Link mode (no EXAM_SERVER) — anywhere public. Claude writes an exam and hands the teacher a link that opens it in
the online editor (or the school server). Usable from Claude on the web and phone; nothing is saved.

    python connector/server.py                    # http://localhost:8000/mcp

Settings (environment variables):
    EXAM_SERVER   school mode: Exam Assistant's address as this server reaches it, e.g. http://127.0.0.1:7900
    EDITOR_URL    school mode: Exam Assistant's address as teachers' browsers reach it (default EXAM_SERVER)
    PUBLIC_URL    this server's own address, e.g. http://8801-openai-01:7901 (links, and the allowed Host header)
    DEMO_URL      link mode: the editor links open in (default: the GitHub Pages demo)
    SCHOOL_URL    link mode: optional "Open on the school server" button, e.g. http://examserver:7900/
    DATA_DIR      links (link mode) and earlier versions (school mode) are kept here (default: connector/data)
    LINK_DAYS     link mode: how long a link works (default 30)
    PORT          default 8000
    TRUST_PROXY   set to 1 behind a reverse proxy, so rate limits use X-Forwarded-For
    STAFF_CODE    testing only: the staff code to use when a request doesn't carry one
"""

from __future__ import annotations

import copy
import functools
import html
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import exam_format  # noqa: E402
import plans as plan_mode  # noqa: E402
import school as school_mode  # noqa: E402
from store import LinkStore  # noqa: E402

ROOT = HERE.parent
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
EXAM_SERVER = os.environ.get("EXAM_SERVER", "").strip().rstrip("/")
EDITOR_URL = os.environ.get("EDITOR_URL", "").strip() or EXAM_SERVER
DEMO_URL = os.environ.get("DEMO_URL", "https://fcl-soc.github.io/exam-builder/")
SCHOOL_URL = os.environ.get("SCHOOL_URL", "").strip()
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
LINK_DAYS = float(os.environ.get("LINK_DAYS", "30"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") == "1"
MODE = "school" if EXAM_SERVER else "link"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("exam-connector")

GUIDE = (ROOT / "docs" / "exam-format.md").read_text(encoding="utf-8")
DEFAULT_STYLE_GUIDE = (ROOT / "docs" / "question-style-guide.md").read_text(encoding="utf-8")
DEFAULT_PLAN_GUIDE = (ROOT / "docs" / "lesson-plan-guide.md").read_text(encoding="utf-8")
EXAMPLE = json.loads((ROOT / "examples" / "sample_exam.json").read_text(encoding="utf-8"))


def format_text(style_guide: str) -> str:
    """What get_exam_format returns: how exams are stored, how this school writes questions, an example, the schema."""
    return (f"{GUIDE}\n\n# Question style guide\n\nFollow this when writing or changing questions.\n\n"
            f"{style_guide.strip() or DEFAULT_STYLE_GUIDE}\n\n"
            f"# Complete example\n\n```json\n{json.dumps(EXAMPLE, indent=1)}\n```\n\n"
            f"# JSON Schema\n\n```json\n{json.dumps(exam_format.SCHEMA, separators=(',', ':'))}\n```\n")

PLAN_FORMAT = """\
# Lesson plans

A lesson plan has details (class_code, subject, year_level, topic, lesson_date as YYYY-MM-DD) and five sections,
L, E, A, R and N, each a short piece of text in the small format below. The teacher sees them as a table, and can
copy it straight into Compass or download it as a Word document.

"""

USE_THE_TOOLS = """\
Whenever the teacher asks for an exam, test, quiz, SAC, assessment or practice questions, make it with these tools.
Never write it out in the chat instead: the teacher needs it in Exam Assistant to edit and print."""

TALKING_TO_TEACHERS = """\
The teacher is not technical. Talk about the exam the way it looks on the page ("Section B, question 2, part b"), never
about ids, JSON, tools, formats, validation or errors you fixed along the way. If something can't be done, say so
plainly and say what they can do instead. Keep replies short: what you did, the link, and anything they should check."""

WRITING_RULES = """\
Follow the question style guide in get_exam_format: command terms, marks that match the number of points a full
answer needs, answer space to suit the marks, and four-option multiple choice with plausible distractors.
Marks go on the deepest parts only. Never type question numbers, part letters or marks into text; they are
automatic. Money is written \\$12.50 (a bare $ starts maths). Graph expressions are in x and use ^."""

LINK_INSTRUCTIONS = f"""\
Writes exams, tests and SACs that open in Exam Assistant, a school's A4 exam editor, ready to review and print.
{USE_THE_TOOLS}

1. Call get_exam_format once before writing an exam: the JSON format, the authoring rules and a full example.
2. Write the exam as JSON. {WRITING_RULES}
3. Call check_exam and fix every error. Treat warnings as advice.
4. Call create_exam_link and give the teacher the link. Opening it shows the exam in the editor, where they can
   change anything before printing. Mention that the online editor doesn't save: print or save as PDF, or use the
   school server option if the link offers one.
Images can't be included; say where a diagram or photo should go and the teacher can add it in the editor.

{TALKING_TO_TEACHERS}
"""

SCHOOL_INSTRUCTIONS = f"""\
Creates and edits exams, tests, quizzes and SACs in this school's Exam Assistant, the A4 exam editor the teacher
uses in their browser. Changes you save appear in their open editor within a couple of seconds, so they can watch
and adjust as you go. {USE_THE_TOOLS}

A worksheet is the same thing with assessment_type "Worksheet" (and task e.g. "Worksheet", class_code e.g.
"10MM1"): no cover page, the class details in a header on page 1.

New exam or worksheet: call get_exam_format once, then build it in steps so the teacher watches it appear:
create_exam with the cover details and the first section, then edit_exam with one add_section per further section.
The editor opens on the teacher's computer after the first call; also give them the editor_link in case it doesn't.

Existing exam: list_exams to find it, get_exam for its outline and ids, then edit_exam with only the changes
asked for (one call can carry many changes). Address sections, questions and parts by id, never by number:
numbers shift when things move. Use the outline returned by each edit for the next one.

The teacher may be editing at the same moment. If you both change the same question, their version wins, so call
get_exam again before changing a question they have been working on.

{WRITING_RULES}
Existing images appear as refs ("img-…") that you can keep, move or remove. New images can only be added by the
teacher in the editor; say where one should go. restore_version undoes your last change if it was wrong.

Lesson plans (LEARN framework, Victorian Curriculum 2.0) are in the same app: call get_lesson_plan_format once and
follow it. Before writing a new plan, list_lesson_plans: if this lesson already has one, offer to change it rather
than making a second; read the class's previous plan so today's builds on it. Use any files or links the teacher
shares. Then create_lesson_plan, or edit_lesson_plan for an existing one (it replaces whole sections, so send only
the sections asked about; the rest are kept). Give the teacher the editor_link, where they can copy the plan into
Compass or download it as a Word document.

{TALKING_TO_TEACHERS}
"""

mcp = MCPServer(name="exam-assistant", title="Exam Assistant",
                instructions=SCHOOL_INSTRUCTIONS if MODE == "school" else LINK_INSTRUCTIONS,
                website_url="https://github.com/FCL-SOC/exam-builder", version="2.0.0")

ExamArg = Annotated[dict[str, Any], Field(description="The whole exam as JSON, in the format from get_exam_format.")]
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


@mcp.tool(title="Get the exam format", annotations=READ)
def get_exam_format() -> str:
    """How to write an exam: the format, the school's question style guide (command terms, marks, wording, answer
    space), a complete example and the JSON Schema. Call this once before writing or changing questions."""
    if MODE == "school":
        try:
            return format_text(school.server.settings().get("style_guide", ""))
        except school_mode.SchoolError:
            pass  # the guide that ships with the app will do
    return format_text(DEFAULT_STYLE_GUIDE)


def _date(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return f"{dt.day} {dt:%B %Y}"


# ------------------------------------------------------------------ school mode
if MODE == "school":
    school = school_mode.School(school_mode.ExamServer(EXAM_SERVER), school_mode.History(DATA_DIR / "history.db"),
                                EDITOR_URL)

    def staff_code(ctx: Context) -> str:
        """The teacher's code, sent by their Claude Desktop extension. Like the editor's, it is not a password."""
        raw = (ctx.headers or {}).get("x-staff-code") or os.environ.get("STAFF_CODE", "")
        code = raw.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ToolError("Your staff code isn't set up. In Exam Assistant, type your staff code, click Use with Claude, "
                            "and install the download again: your code is filled in for you.")
        return code

    def guarded(fn):
        """SchoolError → a tool error Claude can read out, rather than a crash."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except school_mode.SchoolError as e:
                raise ToolError(str(e)) from e
        return wrapper

    @mcp.tool(title="List my exams", annotations=READ)
    @guarded
    def list_exams(ctx: Context) -> dict[str, Any]:
        """The teacher's exams, most recently changed first: exam_id, title and details."""
        owner = staff_code(ctx)
        rows = school.server.list(owner)[:50]
        return {"staff_code": owner, "exams": [
            {"exam_id": r["uid"], "title": r.get("title") or "Untitled exam", "subject": r.get("subject") or "",
             "assessment_type": r.get("assessment_type"), "year_level": r.get("year_level"),
             "total_marks": r.get("total_marks"), "updated": (r.get("updated_at") or "")[:16].replace("T", " ")}
            for r in rows]}

    @mcp.tool(title="Read an exam", annotations=READ)
    @guarded
    def get_exam(exam_id: str, ctx: Context) -> dict[str, Any]:
        """An exam's numbered outline (with the ids edit_exam needs), its full JSON, and the link that opens it."""
        owner = staff_code(ctx)
        found = school.read(owner, exam_id)
        if found["owner"] == owner and exam_format.ensure_ids(copy.deepcopy(found["exam"])):
            # An older exam: give it ids first. Not an undo step: nothing visible changed.
            school.change(owner, exam_id, lambda exam, images: set(), "ids", keep_history=False)
            found = school.read(owner, exam_id)
        shown, _ = school_mode.hide_images(found["exam"])
        return {"exam_id": exam_id, "editor_link": school.link(exam_id), "read_only": found["owner"] != owner,
                "outline": school_mode.outline(shown, exam_id), "exam": shown}

    @mcp.tool(title="Create an exam", annotations=WRITE)
    @guarded
    def create_exam(exam: ExamArg, ctx: Context) -> dict[str, Any]:
        """Create a new exam, test, quiz, SAC or worksheet in the teacher's library from the whole exam (cover details, sections and questions).
        Returns its exam_id, the outline with ids, and the editor_link for the teacher to open."""
        owner = staff_code(ctx)
        result = exam_format.validate(exam)
        if not result["ok"]:
            return {**result, "message": "Nothing was created. Fix these errors, then call create_exam again."}
        full = exam_format.normalise({**school_mode.defaults(school.server.settings()), **exam}, result["total_marks"])
        school_mode.restore_images(full, {}, "exam")
        uid = secrets.token_hex(16)
        school.server.put(owner, uid, full, base=None)
        log.info("%s created %s (%s marks)", owner, uid, result["total_marks"])
        return {"ok": True, "exam_id": uid, "editor_link": school.link(uid), "outline": school_mode.outline(full, uid),
                "warnings": result["warnings"],
                "message": "Give the teacher the editor_link. Once it is open, changes you make appear there live."}

    @mcp.tool(title="Change an exam", annotations=WRITE)
    @guarded
    def edit_exam(exam_id: str,
                  changes: Annotated[list[school_mode.Change], Field(min_length=1, description=(
                      "Applied in order, all or nothing. Address things by the ids in get_exam's outline."))],
                  ctx: Context) -> dict[str, Any]:
        """Change an existing exam: add, rewrite, remove or move questions and parts; add or change sections; change
        cover details. Only what you change is touched; anything the teacher is editing at the same time is kept.
        Returns the new outline, or the errors if nothing was changed."""
        owner = staff_code(ctx)
        note = ", ".join(c.op for c in changes)
        result = school.change(owner, exam_id, lambda exam, images: school_mode.apply_changes(exam, changes, images),
                               note)
        if result["ok"]:
            log.info("%s edited %s: %s", owner, exam_id, note)
        return result

    @mcp.tool(title="Undo Claude's last change", annotations=WRITE)
    @guarded
    def restore_version(exam_id: str, ctx: Context,
                        steps: Annotated[int, Field(ge=1, le=30, description="1 = before your last change, 2 = before "
                                                                              "the one before, …")] = 1) -> dict[str, Any]:
        """Put an exam back the way it was before your last change (or several). This restores the whole exam, so
        edits the teacher made since then are undone too. It can itself be undone: call it again with steps=1."""
        owner = staff_code(ctx)
        versions = school.history.recent(exam_id, owner)
        if len(versions) < steps:
            raise ToolError(f"Only {len(versions)} earlier version(s) of this exam are kept.")
        target = versions[steps - 1]["exam"]

        def put_back(exam: dict, images: dict) -> set[str]:
            exam.clear()
            exam.update(copy.deepcopy(target))
            return set()

        return school.change(owner, exam_id, put_back, f"restore {steps}")


    # ---------------------------------------------------------- lesson plans
    book = plan_mode.PlanBook(school.server, school.history, EDITOR_URL)
    SectionsArg = Annotated[dict[str, str], Field(description=(
        "Sections by letter (L, E, A, R, N), each the section's whole text in the plan format. Include only the "
        "sections you are writing or changing."))]
    DetailsArg = Annotated[dict[str, str], Field(description=(
        "Any of: class_code (e.g. 10MM1), subject, year_level (e.g. 10), topic, lesson_date (YYYY-MM-DD)."))]

    @mcp.tool(title="Get the lesson plan format", annotations=READ)
    def get_lesson_plan_format() -> str:
        """How to write a lesson plan: what goes in each LEARN section and the formatting that Compass and Word
        understand. Call this once before writing or changing a lesson plan."""
        try:
            guide = school.server.settings().get("plan_guide", "")
        except school_mode.SchoolError:
            guide = ""
        return PLAN_FORMAT + (guide.strip() or DEFAULT_PLAN_GUIDE)

    @mcp.tool(title="List my lesson plans", annotations=READ)
    @guarded
    def list_lesson_plans(ctx: Context) -> dict[str, Any]:
        """The teacher's lesson plans, most recently changed first: plan_id, title, class, date and topic."""
        owner = staff_code(ctx)
        return {"staff_code": owner, "lesson_plans": [
            {"plan_id": r["uid"], "title": r.get("title") or "Untitled lesson plan", "class_code": r.get("class_code"),
             "lesson_date": r.get("lesson_date"), "topic": r.get("topic"),
             "updated": (r.get("updated_at") or "")[:16].replace("T", " ")}
            for r in book.list(owner)[:50]]}

    @mcp.tool(title="Read a lesson plan", annotations=READ)
    @guarded
    def get_lesson_plan(plan_id: str, ctx: Context) -> dict[str, Any]:
        """A lesson plan's details and the text of each section, and the link that opens it."""
        owner = staff_code(ctx)
        found = book.read(owner, plan_id)
        return {"plan_id": plan_id, "editor_link": book.link(plan_id), "read_only": found["owner"] != owner,
                "plan": plan_mode.as_text(found["exam"], plan_id)}

    @mcp.tool(title="Create a lesson plan", annotations=WRITE)
    @guarded
    def create_lesson_plan(details: DetailsArg, sections: SectionsArg, ctx: Context) -> dict[str, Any]:
        """Create a new lesson plan in the teacher's library. Returns its plan_id and the editor_link to give the
        teacher; once it is open, changes you make appear there live."""
        owner = staff_code(ctx)
        result = book.create(owner, details, sections)
        if result["ok"]:
            log.info("%s created lesson plan %s", owner, result["plan_id"])
        return result

    @mcp.tool(title="Change a lesson plan", annotations=WRITE)
    @guarded
    def edit_lesson_plan(plan_id: str, ctx: Context, sections: SectionsArg | None = None,
                         details: DetailsArg | None = None) -> dict[str, Any]:
        """Rewrite whole sections and/or change details of an existing lesson plan. Sections you leave out are
        kept, and so is anything the teacher is typing at the same moment."""
        owner = staff_code(ctx)
        if not sections and not details:
            raise ToolError("Give the sections and/or details to change.")
        note = ", ".join([*(sections or {}), *(details or {})])
        return book.change(owner, plan_id, details or {}, sections or {}, note)

    @mcp.tool(title="Undo Claude's last lesson plan change", annotations=WRITE)
    @guarded
    def restore_lesson_plan(plan_id: str, ctx: Context,
                            steps: Annotated[int, Field(ge=1, le=30, description="1 = before your last change, 2 = "
                                                                                  "before the one before, …")] = 1
                            ) -> dict[str, Any]:
        """Put a lesson plan back the way it was before your last change (or several). Edits the teacher made since
        then are undone too. It can itself be undone: call it again with steps=1."""
        owner = staff_code(ctx)
        versions = school.history.recent(plan_id, owner)
        if len(versions) < steps:
            raise ToolError(f"Only {len(versions)} earlier version(s) of this lesson plan are kept.")
        return book.change(owner, plan_id, {}, {}, f"restore {steps}", replace=versions[steps - 1]["exam"])


# ------------------------------------------------------------------ link mode
else:
    links = LinkStore(DATA_DIR / "links.db", days=LINK_DAYS)

    @mcp.tool(title="Check an exam", annotations=READ)
    def check_exam(exam: ExamArg) -> dict[str, Any]:
        """Check an exam against the editor's rules before making a link. Returns ok, errors (must fix),
        warnings (advice), total_marks and the number of questions."""
        return exam_format.validate(exam)

    @mcp.tool(title="Create a link to the exam", annotations=WRITE)
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


@mcp.prompt(title="Write an exam")
def write_exam(subject: str, year_level: str, topic: str, assessment_type: str = "Test",
               total_marks: str = "40", writing_minutes: str = "50", notes: str = "") -> str:
    """Draft an exam, test or SAC in Exam Assistant."""
    finish = ("Create it in Exam Assistant and give me the link to open."
              if MODE == "school" else "Use the Exam Assistant tools: get the format, check the exam, then give me the link.")
    return (f"Write a {assessment_type} for Year {year_level} {subject} on {topic}: about {total_marks} marks, "
            f"{writing_minutes} minutes of writing time. Use a multiple-choice section followed by short and "
            "extended answer questions unless the notes say otherwise, with a range of difficulty and answer space "
            "that suits each question's marks. Mark the correct option on every multiple-choice question."
            + (f"\n\nNotes from the teacher: {notes}" if notes else "") + f"\n\n{finish}")


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> Response:
    if MODE == "school":
        try:
            school.server.settings()
            return JSONResponse({"ok": True, "mode": MODE, "exam_server": EXAM_SERVER})
        except school_mode.SchoolError as e:
            return JSONResponse({"ok": False, "mode": MODE, "error": str(e)}, status_code=503)
    return JSONResponse({"ok": True, "mode": MODE, "links": links.count()})


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(_: Request) -> Response:
    how = ("Teachers connect through the Exam Assistant extension for Claude Desktop."
           if MODE == "school" else "Add it to Claude as a custom connector.")
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>Exam Assistant connector</title>"
                        f"<p>Exam Assistant connector for Claude ({MODE} mode). {how} "
                        f"MCP endpoint: <code>{html.escape(PUBLIC_URL)}/mcp</code></p>")


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


def allowed_hosts() -> list[str]:
    """This server's own names and addresses: PUBLIC_URL's, plus this machine's names and IP addresses on the
    school network (teachers may reach Exam Assistant, and so the connector, either way)."""
    hosts = {urlsplit(PUBLIC_URL).netloc, "localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*"}
    names = {socket.gethostname(), socket.getfqdn()}
    for name in list(names):
        try:
            names |= {info[4][0] for info in socket.getaddrinfo(name, None)}
        except OSError:
            pass
    for target in ("10.254.254.254", "192.0.2.1", "8.8.8.8"):  # the address the OS would send from; nothing is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect((target, 9))
                names.add(probe.getsockname()[0])
            except OSError:
                pass
    for name in filter(None, names):
        if ":" in name:  # IPv6
            name = f"[{name.split('%')[0]}]"
        hosts |= {name, f"{name}:{PORT}", name.lower(), f"{name.lower()}:{PORT}"}
    return sorted(hosts)


def build_app():
    public = urlsplit(PUBLIC_URL)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts(),
        allowed_origins=[f"{public.scheme}://{public.netloc}", "https://claude.ai", "https://claude.com",
                         "http://localhost:*", "http://127.0.0.1:*"],
    )
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security,
                                  max_request_body_size=1_000_000, host="0.0.0.0")
    return RateLimit(app, limit=600 if MODE == "school" else 120)


app = build_app()

if __name__ == "__main__":
    if MODE == "school":
        log.info("School mode: MCP endpoint %s/mcp, exams at %s (teachers open %s)", PUBLIC_URL, EXAM_SERVER, EDITOR_URL)
    else:
        log.info("Link mode: MCP endpoint %s/mcp; links open %s%s", PUBLIC_URL, DEMO_URL,
                 f" or {SCHOOL_URL}" if SCHOOL_URL else "")
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=TRUST_PROXY, forwarded_allow_ips="*" if TRUST_PROXY else None)
