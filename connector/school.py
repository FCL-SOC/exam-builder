"""
School mode: Claude works directly on exams saved in the school's Exam Assistant.

Everything goes through Exam Assistant's own HTTP API (never its database), with the same version check the
editor uses: a change is read, applied, checked and saved with ?base=<version>; if someone saved in between
(the teacher typing, another tab), it is read and applied again. Questions are addressed by their stable ids,
so re-applying is safe even if the teacher has moved things around meanwhile.

Images stay in the exam but are never sent to Claude: they become short refs ("img-1a2b3c4d") that Claude can
keep, move or remove, and they are put back on save.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, Field

import exam_format


class SchoolError(Exception):
    """Something the teacher or Claude needs to hear about, in words they can act on."""


class Conflict(Exception):
    """The exam was saved somewhere else since it was read."""


# ------------------------------------------------------------------ Exam Assistant's API
class ExamServer:
    def __init__(self, base_url: str, timeout: float = 15) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, **query: str) -> tuple[int, Any]:
        url = f"{self.base}/api/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                try:
                    return e.code, json.loads(e.read() or b"null")
                except ValueError:
                    return e.code, None
        except OSError as e:
            raise SchoolError(f"Exam Assistant isn't answering at {self.base} ({e}). Is it running?") from e

    def settings(self) -> dict:
        status, body = self._call("GET", "settings")
        return body if status == 200 and isinstance(body, dict) else {}

    def list(self, owner: str) -> list[dict]:
        status, body = self._call("GET", "exams", owner=owner)
        if status != 200:
            raise SchoolError((body or {}).get("error") or f"Couldn't list exams (HTTP {status}).")
        return body

    def get(self, owner: str, uid: str) -> dict | None:
        status, body = self._call("GET", f"exams/{urllib.parse.quote(uid)}", owner=owner)
        if status == 404:
            return None
        if status != 200:
            raise SchoolError((body or {}).get("error") or f"Couldn't open the exam (HTTP {status}).")
        return body

    def put(self, owner: str, uid: str, exam: dict, base: str | None) -> str:
        query = {"owner": owner, "by": "claude", **({"base": base} if base else {})}
        status, body = self._call("PUT", f"exams/{urllib.parse.quote(uid)}", exam, **query)
        if status == 409:
            raise Conflict()
        if status == 404:
            raise SchoolError("That exam belongs to another teacher, so it can't be changed.")
        if status != 200:
            raise SchoolError((body or {}).get("error") or f"Couldn't save the exam (HTTP {status}).")
        return body["updated_at"]


# ------------------------------------------------------------------ versions kept before each change
class History:
    """The exam as it was before each of Claude's changes, so a change can be undone from the chat."""

    KEEP_PER_EXAM = 30
    KEEP_DAYS = 30

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, owner TEXT NOT NULL,
                saved_at REAL NOT NULL, note TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_versions_uid ON versions(uid, id);""")

    def add(self, uid: str, owner: str, exam: dict, note: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO versions (uid, owner, saved_at, note, body) VALUES (?, ?, ?, ?, ?)",
                               (uid, owner, time.time(), note, json.dumps(exam)))
            self._conn.execute("DELETE FROM versions WHERE uid = ? AND id NOT IN "
                               "(SELECT id FROM versions WHERE uid = ? ORDER BY id DESC LIMIT ?)",
                               (uid, uid, self.KEEP_PER_EXAM))
            self._conn.execute("DELETE FROM versions WHERE saved_at < ?", (time.time() - self.KEEP_DAYS * 86400,))

    def recent(self, uid: str, owner: str) -> list[dict]:
        """Newest first: [{id, saved_at, note, exam}]."""
        with self._lock:
            rows = self._conn.execute("SELECT id, saved_at, note, body FROM versions WHERE uid = ? AND owner = ? "
                                      "ORDER BY id DESC", (uid, owner)).fetchall()
        return [{"id": r[0], "saved_at": r[1], "note": r[2], "exam": json.loads(r[3])} for r in rows]


# ------------------------------------------------------------------ reading an exam for Claude
ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]


def _label(depth: int, i: int) -> str:
    """The editor's numbering: Question 1, a., i."""
    return f"{i + 1}." if depth == 0 else f"{chr(97 + i)}." if depth == 1 else f"{ROMAN[i] if i < 12 else i + 1}."


def _item_marks(it: dict) -> float:
    parts = it.get("parts") or []
    return sum(_item_marks(p) for p in parts) if parts else (float(it.get("marks") or 0))


def _marks_text(m: float) -> str:
    m = int(m) if m == int(m) else m
    return "1 mark" if m == 1 else f"{m} marks"


def _summary_text(it: dict) -> str:
    for b in it.get("blocks") or []:
        if b.get("type") == "text" and str(b.get("value", "")).strip():
            text = re.sub(r"\s+", " ", b["value"]).strip()
            return text if len(text) <= 80 else text[:77] + "…"
    kinds = [b.get("type") for b in it.get("blocks") or []]
    return f"({', '.join(kinds)})" if kinds else "(empty)"


def outline(exam: dict, uid: str) -> str:
    """A numbered list of the exam with ids, as the teacher sees it numbered on the page."""
    sem = exam_format.semantics(exam)
    lines = [f"{exam.get('title') or exam_format.exam_title(exam)} — {_marks_text(sem['total_marks'])}, "
             f"{sem['questions']} questions. Exam id: {uid}"]

    def walk(it: dict, depth: int, i: int) -> None:
        missing = "" if it.get("parts") or (it.get("marks") or 0) > 0 else ", needs marks"
        lines.append(f"{'   ' * (depth + 1)}{_label(depth, i)} ({_marks_text(_item_marks(it))}{missing}) "
                     f"{_summary_text(it)}  [id {it.get('id')}]")
        for j, p in enumerate(it.get("parts") or []):
            walk(p, depth + 1, j)

    for s in exam.get("sections") or []:
        title = f"Section {s.get('name', '')}" + (f": {s['description']}" if s.get("description") else "")
        lines.append(f"{title}  [id {s.get('id')}]")
        for i, q in enumerate(s.get("questions") or []):
            walk(q, 0, i)
    return "\n".join(lines)


def _blocks_in(node: Any):
    """Every block inside a section, question, part or block, including graph/table/image options."""
    if isinstance(node, dict):
        if "type" in node:
            yield node
            for o in node.get("options") or []:
                yield from _blocks_in(o)
        for key in ("sections", "questions", "parts", "blocks"):
            for child in node.get(key) or []:
                yield from _blocks_in(child)


def hide_images(exam: dict) -> tuple[dict, dict[str, str]]:
    """A copy with each image's data swapped for a short ref, and the map to put them back."""
    shown = copy.deepcopy(exam)
    images: dict[str, str] = {}
    for b in _blocks_in(shown):
        if b.get("type") == "image" and str(b.get("value", "")).startswith("data:image/"):
            ref = "img-" + hashlib.sha1(b["value"].encode()).hexdigest()[:8]
            images[ref] = b.pop("value")
            b["ref"] = ref
    return shown, images


def restore_images(node: Any, images: dict[str, str], where: str) -> None:
    """Put image data back in place of refs (in place). Unknown refs are an error: Claude can't add images."""
    for b in _blocks_in(node):
        if b.get("type") == "image" and "ref" in b:
            ref = b.pop("ref")
            if ref not in images:
                raise SchoolError(f"{where}: there is no image {ref!r} in this exam. New images can only be added "
                                  "in the editor; describe where one should go instead.")
            b["value"] = images[ref]


# ------------------------------------------------------------------ changes
class AddItems(BaseModel):
    """Add questions to a section, or parts to a question or part."""
    op: Literal["add"]
    to: str = Field(description="Id of a section (adds questions), a question (adds parts) or a part (adds sub-parts).")
    items: list[dict[str, Any]] = Field(min_length=1, description="The new questions or parts, in the exam format. "
                                                                    "Leave out 'id'; new ones are assigned.")
    after: str | None = Field(None, description="Id of the question or part to put them after. Leave out to add at "
                                                "the end; 'start' puts them first.")


class ReplaceItem(BaseModel):
    """Rewrite one question or part (text, marks, blocks, and its parts). Its id stays the same."""
    op: Literal["replace"]
    id: str = Field(description="Id of the question or part.")
    item: dict[str, Any] = Field(description="The whole new question or part. Include its parts (keep their ids to "
                                             "keep them; parts left out are removed).")


class RemoveItem(BaseModel):
    """Remove a question, part or section."""
    op: Literal["remove"]
    id: str


class MoveItem(BaseModel):
    """Move a question or part, within its list or to another section or question."""
    op: Literal["move"]
    id: str
    to: str = Field(description="Id of the section, question or part it goes into (its current one to reorder).")
    after: str | None = Field(None, description="Id to put it after; leave out for the end, 'start' for first.")


class AddSection(BaseModel):
    op: Literal["add_section"]
    section: dict[str, Any] = Field(description="name (e.g. 'C'), description, instructions, to_answer, questions.")
    after: str | None = Field(None, description="Id of the section to put it after; leave out for the end.")


class UpdateSection(BaseModel):
    """Change a section's heading fields (not its questions)."""
    op: Literal["update_section"]
    id: str
    changes: dict[str, Any] = Field(description="Any of: name, description, instructions, to_answer.")


class UpdateDetails(BaseModel):
    """Change cover details: unit, subject, assessment_type, year_level, semester, year, reading_min, writing_min,
    calculator, task, instructions, learning_area, no_write, show_teacher."""
    op: Literal["update_details"]
    changes: dict[str, Any]


Change = Annotated[Union[AddItems, ReplaceItem, RemoveItem, MoveItem, AddSection, UpdateSection, UpdateDetails],
                   Field(discriminator="op")]

KINDS = ["question", "part", "subpart"]
SECTION_FIELDS = {"name", "description", "instructions", "to_answer"}


def _index(exam: dict) -> dict[str, dict]:
    """id -> {node, kind, list (the list it is in), depth}."""
    found: dict[str, dict] = {}

    def walk(items: list, depth: int) -> None:
        for it in items:
            found[it.get("id")] = {"node": it, "kind": KINDS[depth], "list": items, "depth": depth}
            walk(it.setdefault("parts", []), depth + 1)

    for s in exam["sections"]:
        found[s.get("id")] = {"node": s, "kind": "section", "list": exam["sections"], "depth": -1}
        walk(s.setdefault("questions", []), 0)
    return found


def _ids_in(node: dict) -> set[str]:
    out = {node.get("id")}
    for child in (node.get("questions") or []) + (node.get("parts") or []):
        out |= _ids_in(child)
    return out


def _height(item: dict) -> int:
    """How many levels of parts sit under an item (0 for none)."""
    return 1 + max((_height(p) for p in item.get("parts") or []), default=-1)


def _fresh_ids(node: dict, keep: set[str], taken: set[str]) -> None:
    """Ids for new content: an id is kept only if it is in `keep` (the item's own existing parts); anything else gets
    a new one, so Claude can never make two things share an id."""
    def walk(n: dict) -> None:
        if n.get("id") not in keep or n.get("id") in taken:
            n["id"] = exam_format.new_id()
            while n["id"] in taken:
                n["id"] = exam_format.new_id()
        taken.add(n["id"])
        for child in (n.get("questions") or []) + (n.get("parts") or []):
            walk(child)
    walk(node)


def _place(lst: list, new: list, after: str | None, where: str) -> None:
    if after is None:
        lst.extend(new)
    elif after == "start":
        lst[0:0] = new
    else:
        ids = [x.get("id") for x in lst]
        if after not in ids:
            raise SchoolError(f"{where}: 'after' is {after!r}, which isn't in that list.")
        i = ids.index(after) + 1
        lst[i:i] = new


def _child_kind(target: dict, where: str) -> tuple[str, list]:
    """What can be added into a section/question/part, and the list it goes in."""
    if target["kind"] == "section":
        return "question", target["node"]["questions"]
    if target["kind"] == "subpart":
        raise SchoolError(f"{where}: sub-parts (i., ii.) can't have parts of their own.")
    return KINDS[target["depth"] + 1], target["node"].setdefault("parts", [])


def _prepare(item: dict, kind: str, images: dict, where: str) -> dict:
    """Check a question or part from Claude against the format, then fill in what the editor expects."""
    errors = exam_format.fragment_errors(kind, item, where)
    if errors:
        raise ChangeRejected(errors)
    item = copy.deepcopy(item)
    restore_images(item, images, where)
    return exam_format._item_defaults(item)


class ChangeRejected(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def apply_changes(exam: dict, changes: list[BaseModel], images: dict[str, str]) -> set[str]:
    """Apply changes in order, in place. Returns the ids of everything added or changed ('cover' for the details).
    Raises SchoolError (bad target) or ChangeRejected (content that breaks the format)."""
    touched: set[str] = set()
    for n, ch in enumerate(changes):
        where = f"changes[{n}] ({ch.op})"
        found = _index(exam)
        taken = set(found)

        def target(id_: str, what: str = "id") -> dict:
            if id_ not in found:
                raise SchoolError(f"{where}: no section, question or part has {what} {id_!r} (it may have been "
                                  "deleted; call get_exam for the current ids).")
            return found[id_]

        if isinstance(ch, AddItems):
            kind, dest = _child_kind(target(ch.to), where)
            new = []
            for i, raw in enumerate(ch.items):
                item = _prepare({k: v for k, v in raw.items() if k != "id"}, kind, images, f"{where}.items[{i}]")
                _fresh_ids(item, set(), taken)
                new.append(item)
            _place(dest, new, ch.after, where)
            for item in new:
                touched |= _ids_in(item)
        elif isinstance(ch, ReplaceItem):
            t = target(ch.id)
            if t["kind"] == "section":
                raise SchoolError(f"{where}: {ch.id!r} is a section; use update_section, or replace its questions.")
            item = _prepare(ch.item, t["kind"], images, f"{where}.item")
            own = _ids_in(t["node"]) - {ch.id}
            item["id"] = ch.id
            available = (taken - own) | {ch.id}  # one shared set, so two parts can't both claim the same old id
            for child in item.get("parts") or []:
                _fresh_ids(child, own, available)
            t["list"][t["list"].index(t["node"])] = item
            touched |= _ids_in(item)
        elif isinstance(ch, RemoveItem):
            t = target(ch.id)
            if t["kind"] == "section" and len(exam["sections"]) == 1:
                raise SchoolError(f"{where}: an exam needs at least one section.")
            t["list"].remove(t["node"])
        elif isinstance(ch, MoveItem):
            t = target(ch.id)
            if t["kind"] == "section":
                if ch.to != ch.id and found.get(ch.to, {}).get("kind") != "section":
                    raise SchoolError(f"{where}: to reorder sections, give the section's own id as 'to'.")
                exam["sections"].remove(t["node"])
                _place(exam["sections"], [t["node"]], ch.after, where)
                continue
            if ch.to in _ids_in(t["node"]):
                raise SchoolError(f"{where}: can't move a question into itself.")
            kind, dest = _child_kind(target(ch.to, "'to'"), where)
            if KINDS.index(kind) + _height(t["node"]) > 2:
                raise SchoolError(f"{where}: that would nest parts more than two levels deep (a., then i.).")
            t["list"].remove(t["node"])
            _place(dest, [t["node"]], ch.after, where)
            touched |= _ids_in(t["node"])
        elif isinstance(ch, AddSection):
            raw = {k: v for k, v in ch.section.items() if k != "id"}
            raw.setdefault("questions", [])
            questions = raw.pop("questions")
            section = {"description": "", "to_answer": None, "instructions": "", **raw, "questions": []}
            errors = exam_format.fragment_errors("section", {**section, "questions": questions or [{"blocks": []}]}, where)
            if errors:
                raise ChangeRejected(errors)
            section["questions"] = [_prepare(q, "question", images, f"{where}.section.questions[{i}]")
                                    for i, q in enumerate(questions)]
            _fresh_ids(section, set(), taken)
            if ch.after is not None and found.get(ch.after, {}).get("kind") != "section":
                raise SchoolError(f"{where}: 'after' must be a section id.")
            _place(exam["sections"], [section], ch.after, where)
            touched |= _ids_in(section)
        elif isinstance(ch, UpdateSection):
            t = target(ch.id)
            if t["kind"] != "section":
                raise SchoolError(f"{where}: {ch.id!r} isn't a section.")
            bad = set(ch.changes) - SECTION_FIELDS
            if bad:
                raise SchoolError(f"{where}: sections only have {', '.join(sorted(SECTION_FIELDS))} "
                                  f"(not {', '.join(sorted(bad))}).")
            errors = exam_format.fragment_errors("section", {**t["node"], **ch.changes, "questions": [{"blocks": []}]},
                                                 where)
            if errors:
                raise ChangeRejected(errors)
            t["node"].update(ch.changes)
            touched.add(ch.id)
        elif isinstance(ch, UpdateDetails):
            errors = exam_format.fragment_errors("details", ch.changes, where)
            if errors:
                raise ChangeRejected(errors)
            exam.update(ch.changes)
            touched.add("cover")
    return touched


_PATH = re.compile(r"^sections\[(\d+)\]((?:\.questions\[\d+\])?(?:\.parts\[\d+\])*)")


def _owner_of(exam: dict, message: str) -> str:
    """The id of the deepest section/question/part a semantics() message is about ('cover' for the rest)."""
    m = _PATH.match(message)
    if not m:
        return "cover"
    node = exam["sections"][int(m.group(1))]
    for step in re.findall(r"(questions|parts)\[(\d+)\]", m.group(2)):
        node = node[step[0]][int(step[1])]
    return node.get("id")


def check(exam: dict, touched: set[str]) -> tuple[list[str], list[str], dict]:
    """(errors in what was touched, warnings about what was touched, the full semantics result)."""
    sem = exam_format.semantics(exam)
    mine = lambda msgs: [m for m in msgs if _owner_of(exam, m) in touched]  # noqa: E731
    return mine(sem["errors"]), mine(sem["warnings"]), sem


# ------------------------------------------------------------------ new exams
def defaults(settings: dict) -> dict:
    """The editor's defaults() for a new exam, with the school's own wording from School settings."""
    return {"learning_area": "", "subject": "", "unit": "", "task": settings.get("default_task") or "Written Examination",
            "semester": "1", "year": str(datetime.now().year), "reading_min": 10, "writing_min": 60,
            "calculator": "none", "instructions": settings.get("instructions") or "", "show_teacher": False,
            "shared": False, "no_write": True, "assessment_type": "Exam", "year_level": ""}


def stamp(exam: dict, sem: dict) -> None:
    """What the editor sets on every save, so My exams shows the right title and marks."""
    exam["title"] = exam_format.exam_title(exam)
    exam["total_marks"] = sem["total_marks"]


# ------------------------------------------------------------------ the read → change → save loop
class School:
    def __init__(self, server: ExamServer, history: History, editor_url: str) -> None:
        self.server, self.history = server, history
        self.editor_url = editor_url.rstrip("/") + "/"

    def link(self, uid: str) -> str:
        return f"{self.editor_url}#exam={uid}"

    def read(self, owner: str, uid: str) -> dict:
        found = self.server.get(owner, uid)
        if not found:
            raise SchoolError(f"There's no exam with id {uid!r} in {owner}'s exams. Use list_exams to find it.")
        return found

    def change(self, owner: str, uid: str, mutate: Callable[[dict, dict], set[str]], note: str) -> dict:
        """Read, mutate(exam, images) -> touched ids, check, save with the version check; again on a clash."""
        for _ in range(4):
            current = self.read(owner, uid)
            if current["owner"] != owner:
                raise SchoolError(f"That exam belongs to {current['owner']}, so it can't be changed. "
                                  "Make a copy in the editor first (Duplicate to my exams).")
            before = current["exam"]
            exam = copy.deepcopy(before)
            exam_format.ensure_ids(exam)
            _, images = hide_images(exam)
            try:
                touched = mutate(exam, images)
            except ChangeRejected as e:
                return {"ok": False, "errors": e.errors, "message": "Nothing was changed. Fix these and try again."}
            errors, warnings, sem = check(exam, touched)
            if errors:
                return {"ok": False, "errors": errors, "message": "Nothing was changed. Fix these and try again."}
            stamp(exam, sem)
            try:
                self.server.put(owner, uid, exam, base=current["updated_at"])
            except Conflict:
                continue  # the teacher saved meanwhile: read their version and apply the change again
            self.history.add(uid, owner, before, note)
            shown, _ = hide_images(exam)
            return {"ok": True, "exam_id": uid, "editor_link": self.link(uid), "outline": outline(shown, uid),
                    "warnings": warnings}
        raise SchoolError("The exam kept changing while I tried to save. Try again in a moment.")
