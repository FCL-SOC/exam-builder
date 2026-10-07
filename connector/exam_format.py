"""
The exam format: validation, normalisation and packing for #data= links.

The schema in schema/exam.schema.json checks structure. This module adds the rules a schema can't express,
mirroring what static/index.html does when it draws and prints an exam:

- marks belong on the deepest parts only (the editor ignores marks on anything with parts, and refuses to
  print while a leaf has none);
- graph expressions must parse with the editor's own parser (a bad one is silently not drawn);
- $...$ in text must pair up (an odd one turns the rest of the text into maths).

pack() produces the fragment the editor's #data= import reads: compact JSON, raw DEFLATE, base64url.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import re
import secrets
import zlib
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "exam.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(SCHEMA)

MAX_EXAM_BYTES = 300_000  # an exam without images is a few tens of KB; this only stops abuse
MAX_MESSAGES = 40


# ------------------------------------------------------------------ graph expressions (port of compileExpr)
def _safe(fn: Callable[[float], float]) -> Callable[[float], float]:
    """JavaScript's Math returns NaN or ±Infinity where Python raises; the renderer skips non-finite values."""
    def wrapped(v: float) -> float:
        try:
            return fn(v)
        except (ValueError, OverflowError, ZeroDivisionError):
            return math.nan
    return wrapped


def _log10(v: float) -> float:
    return -math.inf if v == 0 else math.log10(v)


def _ln(v: float) -> float:
    return -math.inf if v == 0 else math.log(v)


# Order matters: it is the order the editor tries names when splitting a run of letters such as "xsinx".
FUNCS: dict[str, Callable[[float], float]] = {
    "sin": _safe(math.sin), "cos": _safe(math.cos), "tan": _safe(math.tan),
    "asin": _safe(math.asin), "acos": _safe(math.acos), "atan": _safe(math.atan),
    "sqrt": _safe(math.sqrt), "abs": _safe(abs), "ln": _safe(_ln), "log": _safe(_log10), "exp": _safe(math.exp),
}
_PREC = {"+": 1, "-": 1, "*": 2, "/": 2, "neg": 3, "^": 4}


class ExprError(ValueError):
    pass


def _is_atom(t: Any) -> bool:
    return isinstance(t, str) and (bool(re.match(r"^[\d.]", t)) or t in ("x", "pi", "e"))


def _binop(op: str, a: float, b: float) -> float:
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                return math.nan if a == 0 else math.copysign(math.inf, a)
            return a / b
        return math.pow(a, b)
    except (ValueError, OverflowError, ZeroDivisionError):
        return math.nan


def compile_expr(src: str) -> Callable[[float], float]:
    """y as a function of x, parsed exactly as the editor parses it. Raises ExprError with the editor's message."""
    text = re.sub(r"^\s*(y|f\s*\(\s*x\s*\))\s*=", "", str(src or ""), flags=re.I)
    text = text.replace("π", "pi").replace("×", "*").replace("·", "*").replace("−", "-").replace("–", "-").lower()
    tokens: list[str] = []
    for t in re.findall(r"\d*\.\d+|\d+|[a-z]+|\S", text):
        if not re.fullmatch(r"[a-z]+", t):
            tokens.append(t)
            continue
        rest = t
        while rest:
            name = next((n for n in [*FUNCS, "pi"] if rest.startswith(n)), rest[0])
            tokens.append(name)
            rest = rest[len(name):]

    out: list[Any] = []
    ops: list[str] = []

    def binary(op: str) -> None:
        while ops and ops[-1] != "(" and ops[-1] not in FUNCS and (
                _PREC[ops[-1]] > _PREC[op] or (_PREC[ops[-1]] == _PREC[op] and op != "^")):
            out.append(ops.pop())
        ops.append(op)

    prev = None
    for t in tokens:
        prev_is_value = prev is not None and (_is_atom(prev) or prev == ")")
        if prev_is_value and (_is_atom(t) or t in FUNCS or t == "("):
            binary("*")  # 2x, 3sin(x), (x+1)(x-1)
        if _is_atom(t):
            out.append(float(t) if re.match(r"^[\d.]", t) else t)
        elif t in FUNCS or t == "(":
            ops.append(t)
        elif t == ")":
            while ops and ops[-1] != "(":
                out.append(ops.pop())
            if not ops:
                raise ExprError("There's a ) without a matching (.")
            ops.pop()
            if ops and ops[-1] in FUNCS:
                out.append(ops.pop())
        elif t in "+-*/^":
            if not prev_is_value:
                if t == "-":
                    ops.append("neg")
                elif t != "+":
                    raise ExprError(f'"{t}" needs something before it.')
            else:
                binary(t)
        else:
            raise ExprError(f'I don\'t understand "{t}".')
        prev = t
    while ops:
        op = ops.pop()
        if op == "(":
            raise ExprError("A ( is missing its ).")
        out.append(op)

    depth = 0
    for t in out:
        if isinstance(t, float) or _is_atom(t):
            depth += 1
        elif t == "neg" or t in FUNCS:
            if depth < 1:
                raise ExprError("Something is missing.")
        else:
            depth -= 1
            if depth < 1:
                raise ExprError("Something is missing next to an operator.")
    if depth != 1:
        raise ExprError("Check the expression." if out else "Type an expression, e.g. x^2 - 2")

    def evaluate(x: float) -> float:
        st: list[float] = []
        for t in out:
            if isinstance(t, float):
                st.append(t)
            elif t == "x":
                st.append(x)
            elif t == "pi":
                st.append(math.pi)
            elif t == "e":
                st.append(math.e)
            elif t == "neg":
                st.append(-st.pop())
            elif t in FUNCS:
                st.append(FUNCS[t](st.pop()))
            else:
                b, a = st.pop(), st.pop()
                st.append(_binop(t, a, b))
        return st[0]

    return evaluate


# ------------------------------------------------------------------ validation
def _path(parts) -> str:
    s = ""
    for p in parts:
        s += f"[{p}]" if isinstance(p, int) else (f".{p}" if s else str(p))
    return s or "(exam)"


def _schema_errors(exam: Any, validator: Draft202012Validator = _VALIDATOR, where: str = "") -> list[str]:
    messages = []
    for err in sorted(validator.iter_errors(exam), key=lambda e: list(map(str, e.absolute_path))):
        if err.validator in ("anyOf", "oneOf") and err.context:
            err = _best_branch_error(err)
        path = _path(err.absolute_path)
        if where:
            path = where if path == "(exam)" else where + ("" if path.startswith("[") else ".") + path
        messages.append(f"{path}: {err.message}")
    return messages


# Parts of an exam on their own, for checking what Claude sends to change one question or section.
_FRAGMENTS = {kind: Draft202012Validator({"$ref": f"#/$defs/{kind}", "$defs": SCHEMA["$defs"]})
              for kind in ("section", "question", "part", "subpart")}
_FRAGMENTS["details"] = Draft202012Validator({
    "type": "object", "additionalProperties": False, "$defs": SCHEMA["$defs"],
    "properties": {k: v for k, v in SCHEMA["properties"].items() if k not in ("sections", "title", "total_marks")}})


def fragment_errors(kind: str, value: Any, where: str) -> list[str]:
    """Schema errors in one section ('section'), question ('question'), part ('part' or 'subpart'), or a set of
    cover fields ('details'), with paths starting at `where`."""
    return _schema_errors(value, _FRAGMENTS[kind], where)


def _best_branch_error(err):
    """For 'a string, or a graph, or a table', report what is wrong with the branch the value was meant to be,
    rather than 'is not of type string'."""
    branches: dict[int, list] = {}
    for sub in err.context:
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    meant = [errs for errs in branches.values()
             if not any(e.validator == "type" and not e.relative_path for e in errs)
             and not any(e.validator == "const" and list(e.relative_path) == ["type"] for e in errs)]
    return best_match([e for errs in meant for e in errs] or err.context)


def _parse_numbers(text: str) -> list[float]:
    vals = []
    for tok in re.split(r"[\s,;]+", str(text or "")):
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            continue
        if math.isfinite(v):
            vals.append(v)
    return vals


# A doubled backslash before $ or before a LaTeX command: escaped twice by mistake (\\frac, \\$25). It prints a stray
# backslash and breaks the maths. A real line break (\\ followed by a space, a newline or another backslash, as in
# aligned equations) is left alone.
_LATEX_COMMANDS = ("frac", "dfrac", "tfrac", "sqrt", "text", "mathrm", "times", "div", "pm", "mp", "cdot", "pi", "theta",
                   "alpha", "beta", "gamma", "delta", "lambda", "mu", "sigma", "le", "leq", "ge", "geq", "ne", "neq",
                   "approx", "circ", "degree", "infty", "sin", "cos", "tan", "log", "ln", "left", "right", "quad",
                   "overline", "vec", "hat", "bar", "angle", "triangle", "parallel", "perp", "begin", "end")
_DOUBLED = re.compile(r"(?<!\\)\\\\(\$|(?:" + "|".join(_LATEX_COMMANDS) + r")(?![A-Za-z]))")


def _doubled_backslash(text: str) -> str | None:
    m = _DOUBLED.search(str(text or ""))
    return m.group(0) if m else None


def _unpaired_dollars(text: str) -> bool:
    return str(text or "").replace("\\$", "").count("$") % 2 == 1


class _Checker:
    def __init__(self, maths: bool = False) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.maths = maths  # VCE maths papers give ruled lines for working and answers, never boxes

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")

    # -- text
    def doubled(self, where: str, text: str) -> None:
        found = _doubled_backslash(text)
        if found:
            single = found[1:]
            self.err(where, f"has a doubled backslash: {found} should be {single}. Use one backslash; JSON encoding of "
                            "the tool call adds the escaping itself. Two print as a stray backslash and break the maths.")

    def rich(self, where: str, text: str) -> None:
        self.doubled(where, text)
        if _unpaired_dollars(text):
            self.err(where, "has an unpaired $. $...$ marks inline maths; write money as \\$12.50 (backslash dollar).")

    # -- blocks
    def block(self, where: str, b: dict, in_option: bool = False) -> None:
        t = b.get("type")
        if t == "text":
            self.rich(where, b.get("value", ""))
        elif t == "equation":
            self.doubled(where, b.get("value", ""))
            if "$" in b.get("value", "").replace("\\$", ""):
                self.err(where, "equation values are LaTeX without $ delimiters.")
        elif t == "table":
            rows = b.get("rows") or []
            widths = {len(r) for r in rows}
            if len(widths) > 1:
                self.err(where, f"every table row must have the same number of cells (found {sorted(widths)}).")
            ncols = max(widths) if widths else 0
            for i in b.get("shade_rows") or []:
                if i >= len(rows):
                    self.err(where, f"shade_rows has {i}, but the table has {len(rows)} rows.")
            for i in b.get("shade_cols") or []:
                if i >= ncols:
                    self.err(where, f"shade_cols has {i}, but the table has {ncols} columns.")
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    self.rich(f"{where}.rows[{ri}][{ci}]", cell)
        elif t == "answer":
            for i, box in enumerate(b.get("boxes") or []):
                self.rich(f"{where}.boxes[{i}].label", box.get("label", ""))
                self.rich(f"{where}.boxes[{i}].units", box.get("units", ""))
        elif t == "choices":
            opts = b.get("options") or []
            correct = b.get("correct")
            if correct is None:
                self.warn(where, "no correct option marked. Set 'correct' (0 = A) so the teacher has the answer.")
            elif correct >= len(opts):
                self.err(where, f"correct is {correct}, but there are only {len(opts)} options (0 = A).")
            for i, o in enumerate(opts):
                ow = f"{where}.options[{i}]"
                if isinstance(o, str):
                    if not o.strip():
                        self.err(ow, "is empty.")
                    self.rich(ow, o)
                else:
                    self.block(ow, o, in_option=True)
        elif t == "graph":
            self.graph(where, b)
        elif t == "image":
            if not str(b.get("value", "")).startswith("data:image/") and not b.get("ref"):
                self.err(where, "an image needs the 'ref' of an image already in this exam (new images can only "
                                "be added in the editor).")

    def axis_ok(self, where: str, a: dict | None, name: str) -> bool:
        if not a:
            self.err(where, f"needs a '{name}' axis.")
            return False
        lo, hi, step = a.get("min"), a.get("max"), a.get("step", 1) or 1
        if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo < hi):
            self.err(f"{where}.{name}", "min must be less than max.")
            return False
        if (hi - lo) / abs(step) > 200:
            self.warn(f"{where}.{name}", "more than 200 grid steps, so no grid or numbers will be drawn. Use a larger step.")
        return True

    def graph(self, where: str, g: dict) -> None:
        kind = g.get("kind", "plot")
        x_ok = self.axis_ok(where, g.get("x"), "x")
        if kind == "boxplot":
            box = g.get("box")
            if not box:
                self.err(where, "a box plot needs 'box' (raw 'data' or min, q1, median, q3, max).")
            elif len(_parse_numbers(box.get("data", ""))) < 2:
                five = [box.get(k) for k in ("min", "q1", "median", "q3", "max")]
                if not all(isinstance(v, (int, float)) for v in five):
                    if not box.get("hidden"):
                        self.err(where, "a box plot needs at least two data values, or all of min, q1, median, q3, max.")
                elif five != sorted(five):
                    self.err(where, "box plot values must satisfy min ≤ q1 ≤ median ≤ q3 ≤ max.")
            return
        y_ok = self.axis_ok(where, g.get("y"), "y")
        if kind == "histogram":
            hist = g.get("hist")
            if not hist:
                self.err(where, "a histogram needs 'hist' with 'counts' or raw 'data'.")
            elif not _parse_numbers(hist.get("data", "")) and not _parse_numbers(hist.get("counts", "")):
                self.err(where, "a histogram needs bar heights in 'counts' (e.g. '2, 5, 8') or raw 'data'.")
            return
        if not (x_ok and y_ok):
            return
        X, Y = g["x"], g["y"]
        for i, f in enumerate(g.get("functions") or []):
            fw = f"{where}.functions[{i}]"
            try:
                fn = compile_expr(f.get("expr", ""))
            except ExprError as e:
                self.err(fw, f"expression {f.get('expr')!r} won't draw: {e} Use ^ for powers and write in x, "
                             "e.g. 3sin(2x), sqrt(x+1), e^(-x), 1/(x-1).")
                continue
            lo = max(X["min"], f["from"]) if f.get("from") is not None else X["min"]
            hi = min(X["max"], f["to"]) if f.get("to") is not None else X["max"]
            if not lo < hi:
                self.err(fw, "its from/to range lies outside the x axis, so nothing is drawn.")
                continue
            ys = [fn(lo + (hi - lo) * k / 200) for k in range(201)]
            finite = [y for y in ys if math.isfinite(y) and abs(y) < 1e6]
            if not finite:
                self.err(fw, f"{f.get('expr')!r} is undefined everywhere on the x axis shown.")
            elif not any(Y["min"] <= y <= Y["max"] for y in finite):
                self.warn(fw, f"{f.get('expr')!r} never comes within the y axis shown ({Y['min']} to {Y['max']}).")
        for i, p in enumerate(g.get("points") or []):
            if not (X["min"] <= p["x"] <= X["max"] and Y["min"] <= p["y"] <= Y["max"]):
                self.warn(f"{where}.points[{i}]", "is outside the axes and will be cut off.")

    # -- questions
    def item(self, where: str, it: dict, depth: int) -> float:
        for i, b in enumerate(it.get("blocks") or []):
            self.block(f"{where}.blocks[{i}]", b)
        parts = it.get("parts") or []
        if parts:
            if it.get("marks") not in (None, 0):
                self.warn(where, "has parts, so its own marks are ignored; marks go on its parts.")
            return sum(self.item(f"{where}.parts[{i}]", p, depth + 1) for i, p in enumerate(parts))
        marks = it.get("marks")
        if not isinstance(marks, (int, float)) or marks <= 0:
            self.err(where, "needs marks (a number above 0). The editor won't print until every question or "
                            "deepest part has marks.")
            return 0
        types = {b.get("type") for b in it.get("blocks") or []}
        has_blank_cell = any(b.get("type") == "table" and any(not c.strip() for r in b.get("rows", []) for c in r)
                             for b in it.get("blocks") or [])
        if not (types & {"lines", "box", "answer", "choices", "graph"} or has_blank_cell):
            self.warn(where, "has no space to answer (lines, box, answer, choices, a graph, or blank table cells).")
        if self.maths and types & {"box", "answer"}:
            self.warn(where, "uses a box or answer box, but VCE maths papers give ruled lines for the working and the "
                             "answer: use a lines block instead (about four lines per mark).")
        return float(marks)


_STRING_FIELDS = ("semester", "year", "year_level")


def coerce(cover: Any) -> Any:
    """Cover fields the editor keeps as strings, accepted as numbers too (semester 2 → "2"), in place."""
    if isinstance(cover, dict):
        for key in _STRING_FIELDS:
            if isinstance(cover.get(key), int) and not isinstance(cover.get(key), bool):
                cover[key] = str(cover[key])
    return cover


def validate(exam: Any) -> dict:
    """{ok, errors, warnings, total_marks, questions}. ok means it opens and prints cleanly in the editor."""
    if not isinstance(exam, dict):
        return {"ok": False, "errors": ["(exam): must be a JSON object."], "warnings": [], "total_marks": 0, "questions": 0}
    coerce(exam)
    size = len(json.dumps(exam, separators=(",", ":")).encode())
    if size > MAX_EXAM_BYTES:
        return {"ok": False, "errors": [f"(exam): {size // 1000} KB is over the {MAX_EXAM_BYTES // 1000} KB limit."],
                "warnings": [], "total_marks": 0, "questions": 0}
    errors = _schema_errors(exam)
    if errors:  # semantic checks assume the structure is right
        return {"ok": False, "errors": errors[:MAX_MESSAGES], "warnings": [], "total_marks": 0, "questions": 0}
    result = semantics(exam)
    return {**result, "errors": result["errors"][:MAX_MESSAGES], "warnings": result["warnings"][:MAX_MESSAGES]}


def semantics(exam: dict) -> dict:
    """The rules the editor applies when it draws and prints, over a structurally sound exam (one from the editor,
    or one that passed the schema). Same result shape as validate(); messages are not truncated."""
    c = _Checker(maths=bool(re.search(r"math", f"{exam.get('subject') or ''} {exam.get('learning_area') or ''}", re.I)))
    total = 0.0
    questions = 0
    if exam.get("assessment_type") == "Worksheet" and len(exam["sections"]) > 1:
        c.warn("sections[1]", "a worksheet shows no sections (its questions are numbered straight through): keep all "
                              "its questions in one section.")
    names = [s["name"] for s in exam["sections"]]
    if len(set(names)) != len(names):
        c.warn("sections", f"section names repeat ({', '.join(names)}).")
    for si, s in enumerate(exam["sections"]):
        sw = f"sections[{si}]"
        c.rich(f"{sw}.instructions", s.get("instructions", ""))
        if s.get("to_answer") is not None and s["to_answer"] > len(s["questions"]):
            c.err(f"{sw}.to_answer", f"is {s['to_answer']}, but the section has {len(s['questions'])} questions.")
        for qi, q in enumerate(s["questions"]):
            total += c.item(f"{sw}.questions[{qi}]", q, 0)
            questions += 1
    total = int(total) if total == int(total) else total
    return {"ok": not c.errors, "errors": c.errors, "warnings": c.warnings, "total_marks": total, "questions": questions}


# ------------------------------------------------------------------ normalise and pack
def _graph_defaults(g: dict, in_option: bool) -> dict:
    kind = g.get("kind", "plot")
    out = {"type": "graph", "kind": kind, "width": 100 if in_option else 70, "grid": True, "numbers": True,
           "fit": False, "functions": [], "points": [], **g}
    label_x, label_y = ("", "") if kind == "boxplot" else ("x", "Frequency" if kind == "histogram" else "y")
    out["x"] = {"step": 1, "label": label_x, **g["x"]}
    out["y"] = {"min": -5, "max": 5, "step": 1, "label": label_y, **(g.get("y") or {})}
    out["functions"] = [{"from": None, "to": None, "dashed": False, **f} for f in out["functions"]]
    return out


def _block_defaults(b: dict, in_option: bool = False) -> dict:
    t = b["type"]
    if t == "graph":
        return _graph_defaults(b, in_option)
    if t == "table":
        return {"header": False, **b}
    if t == "answer":
        return {**b, "boxes": [{"label": "", "units": "", **x} for x in b["boxes"]]}
    if t == "choices":
        return {"correct": None, **b,
                "options": [o if isinstance(o, str) else _block_defaults(o, True) for o in b["options"]]}
    return dict(b)


def _item_defaults(it: dict) -> dict:
    parts = [_item_defaults(p) for p in it.get("parts") or []]
    return {**it, "marks": None if parts else it.get("marks"), "blocks": [_block_defaults(b) for b in it["blocks"]],
            "parts": parts}


def exam_title(ex: dict) -> str:
    """The editor's examTitle(): 'Probability Year 10 Mathematics · Test S1 2026'."""
    bits = [ex.get("unit"), ex.get("subject"), "·", ex.get("task"), f"S{ex.get('semester', '1')} {ex.get('year', '')}".strip()]
    text = " ".join(str(b) for b in bits if b)
    return re.sub(r"^· ", "", text) or "Untitled exam"


def new_id() -> str:
    """Same shape as the editor's newId(): 8 hex characters."""
    return secrets.token_hex(4)


def ensure_ids(exam: dict) -> int:
    """Give every section, question and part a unique id, as the editor's ensureIds() does. Returns how many were added."""
    seen: set[str] = set()
    added = 0

    def visit(obj: dict) -> None:
        nonlocal added
        if not isinstance(obj.get("id"), str) or not obj["id"] or obj["id"] in seen:
            obj["id"] = new_id()
            while obj["id"] in seen:
                obj["id"] = new_id()
            added += 1
        seen.add(obj["id"])

    def walk(item: dict) -> None:
        visit(item)
        for part in item.get("parts") or []:
            walk(part)

    for section in exam.get("sections") or []:
        visit(section)
        for q in section.get("questions") or []:
            walk(q)
    return added


def normalise(exam: dict, total_marks: float) -> dict:
    """A copy with every field the editor reads filled in. School-wide defaults (task, instructions) stay absent so
    the editor fills them from School settings."""
    ex = copy.deepcopy(exam)
    ex.pop("title", None)
    ex["shared"] = False
    ex["sections"] = [{"description": "", "to_answer": None, "instructions": "", **s,
                       "questions": [_item_defaults(q) for q in s["questions"]]} for s in ex["sections"]]
    ex["total_marks"] = total_marks
    ex["title"] = exam_title(ex)
    ensure_ids(ex)
    return ex


def pack(exam: dict) -> str:
    """Compact JSON → raw DEFLATE → base64url, as the editor's #data= import expects."""
    raw = json.dumps(exam, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = comp.compress(raw) + comp.flush()
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
