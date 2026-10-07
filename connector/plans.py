"""
Lesson plans for Claude, in school mode: the same read → change → save loop as exams, on Exam Assistant's
/api/plans. A plan is five LEARN sections of short markdown plus a few details, so a change is simply "these
sections now say this"; the editor merges it with whatever the teacher is typing (their own section wins).
"""

from __future__ import annotations

import copy
import re
import secrets
from typing import Any

import exam_format
from school import Conflict, ExamServer, History, SchoolError

SECTIONS = {"L": "Learning Clarity", "E": "Explain", "A": "Application", "R": "Reflect", "N": "Next Steps"}
DETAILS = {"class_code": 20, "subject": 80, "year_level": 10, "topic": 160, "lesson_date": 10}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MATHS = re.compile(r"\$\$[^$]+\$\$|\$[^$]+\$")
_LATEX_OUTSIDE = re.compile(r"\\(?:" + "|".join(exam_format._LATEX_COMMANDS) + r")(?![A-Za-z])|\\[(\[]")
_L_HEADINGS = ("Learning Intentions", "Success Criteria", "Do Now")


def blank() -> dict:
    """What the lesson plan page starts a new plan with."""
    return {**{k: "" for k in DETAILS}, "sections": {k: "" for k in SECTIONS}}


def title(plan: dict) -> str:
    """As the page sets it: "10MM1 · Completing the square"."""
    return " · ".join(x for x in (plan.get("class_code"), plan.get("topic")) if x) or "Untitled lesson plan"


def check_section(key: str, text: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one section's text: what would break in the page, Compass or Word."""
    where = f"{key} ({SECTIONS.get(key, '?')})"
    if key not in SECTIONS:
        return [f"{key!r} isn't a section. The sections are L, E, A, R and N."], []
    if not isinstance(text, str):
        return [f"{where}: must be text."], []
    if len(text) > 6000:
        return [f"{where}: is too long ({len(text)} characters); keep sections short."], []
    errors, warnings = [], []
    found = exam_format._doubled_backslash(text)
    if found:
        errors.append(f"{where}: has a doubled backslash: {found} should be {found[1:]}.")
    if exam_format._unpaired_dollars(text):
        errors.append(f"{where}: has an unpaired $. Maths goes in $...$; write money as \\$12.50.")
    for line in text.split("\n"):
        if line.count("$$") % 2:
            errors.append(f"{where}: $$ display maths must open and close on the same line: {line.strip()[:60]!r}")
        if re.match(r"\s*#", line):
            errors.append(f"{where}: no # headings; use a **bold** line instead.")
        elif re.match(r"\s+(?:- |\d+\. )", line):
            errors.append(f"{where}: bullets and numbers start at the beginning of the line (no indenting).")
    outside = _LATEX_OUTSIDE.search(_MATHS.sub("", text.replace("\\$", "")))
    if outside:
        errors.append(f"{where}: {outside.group(0)} is outside $ signs; put the maths inside $...$.")
    for m in _MATHS.finditer(text.replace("\\$", "")):
        if m.group(0).count("=") >= 3:
            warnings.append(f"{where}: {m.group(0)[:50]!r} chains several steps; put each step on its own line.")
    if key == "L" and text.strip():
        missing = [h for h in _L_HEADINGS if f"**{h}**" not in text]
        if missing:
            warnings.append(f"L: the guide asks for bold headings {', '.join(missing)}.")
    if key == "R" and text.strip() and len(re.findall(r"^- ", text, re.M)) != 2:
        warnings.append("R: the guide asks for exactly two bullet points.")
    return errors, warnings


def check_details(details: dict) -> list[str]:
    errors = []
    for k, v in details.items():
        if k not in DETAILS:
            errors.append(f"{k!r} isn't a detail. The details are {', '.join(DETAILS)}.")
        elif not isinstance(v, str) or len(v) > DETAILS[k]:
            errors.append(f"{k}: must be text of at most {DETAILS[k]} characters.")
        elif k == "lesson_date" and v and not _DATE.match(v):
            errors.append("lesson_date: write it as YYYY-MM-DD, e.g. 2026-10-14.")
    return errors


def check(details: dict, sections: dict) -> tuple[list[str], list[str]]:
    errors, warnings = check_details(details), []
    for k, text in sections.items():
        e, w = check_section(k, text)
        errors += e
        warnings += w
    return errors, warnings


def as_text(plan: dict, uid: str) -> str:
    """The plan as Claude reads it: details, then each section under its letter."""
    head = ", ".join(f"{k}: {plan.get(k)}" for k in DETAILS if plan.get(k))
    lines = [f"{title(plan)}  (plan id {uid}){' — ' + head if head else ''}"]
    for k, name in SECTIONS.items():
        text = (plan.get("sections") or {}).get(k, "").strip()
        lines += [f"\n## {k}: {name}", text or "(empty)"]
    return "\n".join(lines)


class PlanBook:
    """The teacher's lesson plans, through Exam Assistant's API."""

    def __init__(self, server: ExamServer, history: History, editor_url: str) -> None:
        self.server, self.history = server, history
        self.editor_url = editor_url.rstrip("/") + "/plans.html"

    def link(self, uid: str) -> str:
        return f"{self.editor_url}?plan={uid}"  # not #: some in-app browsers drop it

    def list(self, owner: str) -> list[dict]:
        return self.server.list(owner, kind="plans")

    def read(self, owner: str, uid: str) -> dict:
        found = self.server.get(owner, uid, kind="plans")
        if not found:
            raise SchoolError(f"There's no lesson plan with id {uid!r} in {owner}'s plans. Use list_lesson_plans.")
        return found

    def _done(self, uid: str, plan: dict, warnings: list[str]) -> dict:
        return {"ok": True, "plan_id": uid, "editor_link": self.link(uid), "plan": as_text(plan, uid),
                "warnings": warnings}

    def create(self, owner: str, details: dict, sections: dict) -> dict:
        errors, warnings = check(details, sections)
        if errors:
            return {"ok": False, "errors": errors, "message": "Nothing was created. Fix these and try again."}
        plan = blank()
        plan.update(details)
        plan["sections"].update(sections)
        plan["title"] = title(plan)
        uid = secrets.token_hex(16)
        self.server.put(owner, uid, plan, base=None, kind="plans")
        return self._done(uid, plan, warnings)

    def change(self, owner: str, uid: str, details: dict, sections: dict, note: str,
               replace: dict | None = None) -> dict:
        """Read, apply, save with the version check; again if the teacher saved meanwhile. `replace` puts back a
        whole earlier version instead. The version before is kept for restore_lesson_plan."""
        errors, warnings = check(details, sections)
        if errors:
            return {"ok": False, "errors": errors, "message": "Nothing was changed. Fix these and try again."}
        for _ in range(4):
            current = self.read(owner, uid)
            if current["owner"] != owner:
                raise SchoolError(f"That lesson plan belongs to {current['owner']}, so it can't be changed.")
            before = current["exam"]
            plan = copy.deepcopy(replace) if replace is not None else {**blank(), **copy.deepcopy(before)}
            plan.update(details)
            plan["sections"] = {**blank()["sections"], **(plan.get("sections") or {}), **sections}
            plan["title"] = title(plan)
            try:
                self.server.put(owner, uid, plan, base=current["updated_at"], kind="plans")
            except Conflict:
                continue
            self.history.add(uid, owner, before, note)
            return self._done(uid, plan, warnings)
        raise SchoolError("The lesson plan kept changing while I tried to save. Try again in a moment.")
