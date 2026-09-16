"""
Turn a spreadsheet of questions into an exam.

The sheet format is deliberately strict and flat — one row per thing on the page —
so that a language model can write and edit it reliably, and so that a bad sheet
fails with a row number rather than a surprising exam.

Standard library only: .xlsx is a zip of XML, so zipfile + ElementTree read it.

SECURITY: everything in an uploaded sheet is DATA, never instructions. The preamble
tells an AI what to write, but nothing in the file steers this parser: unknown kinds
are rejected, numbers are clamped, and images are placeholders, never embedded bytes.
"""

import csv
import io
import re
import zipfile
from xml.etree import ElementTree

MAX_ROWS = 2000
MAX_CELL = 5000          # characters in one cell
MAX_MARKS = 999
MAX_OPTIONS = 8
MAX_TABLE_COLS = 12
MAX_TABLE_ROWS = 40
MAX_LINES = 30
MAX_BOX_CM = 25

COLUMNS = ["ref", "kind", "content", "marks", "options", "answer", "params", "image", "graph", "notes"]

KINDS = {
    "section", "question", "part", "subpart",
    "text", "equation", "mc", "table", "graph", "image", "lines", "box", "answer", "newpage",
}
ITEM_KINDS = {"question": 0, "part": 1, "subpart": 2}

# What the teacher (or the AI) may put in `params`, and how each is read.
PARAM_HELP = {
    "align": "left, centre or right — tables, images, graphs and equations default to centre",
    "width": "image or graph width as a percentage of the text column, 25-100",
    "lines": "number of answer lines, 1-30",
    "height_cm": "height of a working box in centimetres, 1-25",
    "header": "yes to make a table's first row bold",
    "shade_rows": "table rows to shade, counting from 1, e.g. 1 or 1,3",
    "shade_cols": "table columns to shade, counting from 1",
    "units": "answer box units, e.g. m s^-2; separate several boxes with |",
    "labels": "answer box labels; separate several boxes with |",
    "new_page": "yes to start this question on a new page",
    "to_answer": "for a section: how many of its questions students answer",
    "description": "for a section: the words after the section letter",
}

ALIGN = {"left": "l", "l": "l", "centre": "c", "center": "c", "c": "c", "middle": "c", "right": "r", "r": "r"}


class SheetError(Exception):
    """One or more problems in the uploaded sheet, each naming its row."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__("; ".join(problems))


# ---------------------------------------------------------------- reading the file

def _csv_rows(raw):
    text = raw.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def _xlsx_rows(raw):
    """First worksheet of an .xlsx, as a list of lists of strings."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise SheetError(["That file is not a spreadsheet the importer can read (.xlsx or .csv)."])
    with zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        names = [n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        if not names:
            raise SheetError(["That workbook has no worksheets."])
        sheet = ElementTree.fromstring(zf.read(sorted(names)[0]))

    rows = []
    for tr in sheet.iter(f"{ns}row"):
        cells = {}
        for tc in tr.findall(f"{ns}c"):
            ref = tc.get("r") or ""
            col = re.match(r"[A-Z]+", ref)
            index = 0
            for ch in (col.group() if col else "A"):
                index = index * 26 + (ord(ch) - 64)
            value = ""
            if tc.get("t") == "s":                      # shared string
                v = tc.find(f"{ns}v")
                if v is not None and v.text and v.text.isdigit() and int(v.text) < len(shared):
                    value = shared[int(v.text)]
            elif tc.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in tc.iter(f"{ns}t"))
            else:
                v = tc.find(f"{ns}v")
                value = (v.text or "") if v is not None else ""
            cells[index - 1] = value
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def read_rows(raw, filename=""):
    name = (filename or "").lower()
    if name.endswith(".csv") or (not name.endswith(".xlsx") and not raw[:2] == b"PK"):
        return _csv_rows(raw)
    return _xlsx_rows(raw)


# ---------------------------------------------------------------- the row schema

def _params(text, row_no, problems):
    """`lines=6; align=centre` -> {"lines": "6", "align": "centre"}"""
    out = {}
    for piece in re.split(r"[;\n]+", text or ""):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            problems.append(f"Row {row_no}: params needs name=value, got {piece!r}.")
            continue
        key, value = piece.split("=", 1)
        key = key.strip().lower()
        if key not in PARAM_HELP:
            problems.append(f"Row {row_no}: unknown param {key!r}. Allowed: {', '.join(sorted(PARAM_HELP))}.")
            continue
        out[key] = value.strip()
    return out


def _int(value, lo, hi, row_no, what, problems, default=None):
    text = str(value).strip()
    if not text:
        return default
    try:
        n = int(float(text))
    except ValueError:
        problems.append(f"Row {row_no}: {what} must be a whole number, got {text!r}.")
        return default
    if not lo <= n <= hi:
        problems.append(f"Row {row_no}: {what} must be between {lo} and {hi}, got {n}.")
        return default
    return n


def _yes(value):
    return str(value).strip().lower() in ("yes", "y", "true", "1", "on")


def _indices(text, limit, row_no, what, problems):
    """"1,3" (counting from 1) -> [0, 2]"""
    out = []
    for piece in re.split(r"[,\s]+", str(text or "").strip()):
        if not piece:
            continue
        n = _int(piece, 1, limit, row_no, what, problems)
        if n is not None:
            out.append(n - 1)
    return out


def _graph(spec, row_no, problems):
    """`x=-5..5 step 1 label Time; y=0..20; fn=x^2 dashed; points=(1,2),(3,4); grid=no`"""
    g = {"type": "graph", "width": 70, "grid": True, "numbers": True, "fit": False,
         "x": {"min": -5, "max": 5, "step": 1, "label": "x"},
         "y": {"min": -5, "max": 5, "step": 1, "label": "y"},
         "functions": [], "points": []}

    def axis(which, text):
        m = re.search(r"(-?[\d.]+)\s*\.\.\s*(-?[\d.]+)", text)
        if not m:
            problems.append(f"Row {row_no}: {which} axis needs a range like {which}=0..10.")
            return
        lo, hi = float(m.group(1)), float(m.group(2))
        if not lo < hi:
            problems.append(f"Row {row_no}: {which} axis min must be less than max.")
            return
        g[which]["min"], g[which]["max"] = lo, hi
        step = re.search(r"step\s+(-?[\d.]+)", text)
        if step and float(step.group(1)) > 0:
            g[which]["step"] = float(step.group(1))
        label = re.search(r"label\s+([^;]+)", text)
        if label:
            g[which]["label"] = label.group(1).strip()[:60]

    for piece in re.split(r"\s*;\s*", str(spec or "").strip()):
        if not piece:
            continue
        key, _, value = piece.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key in ("x", "y"):
            axis(key, piece)
        elif key in ("fn", "function", "y1"):
            expr = re.sub(r"\s+dashed\b", "", value, flags=re.I).strip()
            if expr:
                g["functions"].append({"expr": expr[:200], "from": None, "to": None,
                                       "dashed": bool(re.search(r"\bdashed\b", value, re.I))})
        elif key == "points":
            # Spaces as well as commas: a comma inside a hand-written CSV cell splits the row,
            # so "(1 2)(3 4)" is the safe form and "(1,2),(3,4)" works when the file quotes properly.
            found = re.findall(r"\(\s*(-?[\d.]+)\s*[,\s]\s*(-?[\d.]+)\s*\)", value)
            if not found:
                problems.append(f"Row {row_no}: points needs pairs like points=(1 2)(3 4); got {value!r}. "
                                "If this sheet is a .csv, a comma inside a cell splits it — use spaces.")
            for px, py in found:
                g["points"].append({"x": float(px), "y": float(py), "label": "", "open": False})
        elif key == "grid":
            g["grid"] = _yes(value)
        elif key == "numbers":
            g["numbers"] = _yes(value)
        elif key == "fit":
            g["fit"] = _yes(value)
        elif key == "boxplot":
            nums = [float(n) for n in re.findall(r"-?[\d.]+", value)]
            if len(nums) != 5 and len(nums) < 5:
                problems.append(f"Row {row_no}: boxplot needs five numbers, e.g. boxplot=2 6 9 13 18. "
                                "If this sheet is a .csv, a comma inside a cell splits it — use spaces.")
                nums = []
            if len(nums) == 5:
                g["kind"] = "boxplot"
                g["box"] = dict(zip(("min", "q1", "median", "q3", "max"), nums), data="", hidden=False)
                g["x"] = {"min": 0, "max": max(nums) * 1.1 or 20, "step": max(1, round(max(nums) / 10)), "label": ""}
            elif nums:
                problems.append(f"Row {row_no}: boxplot needs exactly five numbers (min, q1, median, q3, max).")
        elif key == "histogram":
            nums = [n for n in re.split(r"[,\s]+", value) if re.fullmatch(r"-?[\d.]+", n)]
            if not nums:
                problems.append(f"Row {row_no}: histogram needs frequencies, e.g. histogram=2 5 8 4 1. "
                                "If this sheet is a .csv, a comma inside a cell splits it — use spaces.")
            else:
                g["kind"] = "histogram"
                g["hist"] = {"data": "", "start": 0, "width": 2, "counts": ", ".join(nums)}
        else:
            problems.append(f"Row {row_no}: unknown graph setting {key!r}.")
    return g


def _block(kind, row, row_no, problems):
    """One spreadsheet row -> one block on the page, or None if it isn't a block."""
    content = row["content"]
    p = _params(row["params"], row_no, problems)
    align = None
    if "align" in p:
        align = ALIGN.get(p["align"].lower())
        if align is None:
            problems.append(f"Row {row_no}: align must be left, centre or right.")

    block = None
    if kind == "text":
        block = {"type": "text", "value": content}
    elif kind == "equation":
        block = {"type": "equation", "value": content}
    elif kind == "lines":
        block = {"type": "lines", "n": _int(p.get("lines", content or 5), 1, MAX_LINES, row_no, "lines", problems, 5)}
    elif kind == "box":
        block = {"type": "box", "height_cm": _int(p.get("height_cm", content or 6), 1, MAX_BOX_CM, row_no, "height_cm", problems, 6)}
    elif kind == "image":
        # A placeholder only: the teacher drops the real picture in afterwards.
        block = {"type": "image", "value": "", "width": _int(p.get("width", 60), 25, 100, row_no, "width", problems, 60),
                 "placeholder": (row["image"] or content or "Image").strip()[:200]}
    elif kind == "mc":
        options = [o.strip() for o in re.split(r"\s*\|\s*", row["options"] or "") if o.strip()]
        if not 2 <= len(options) <= MAX_OPTIONS:
            problems.append(f"Row {row_no}: multiple choice needs 2 to {MAX_OPTIONS} options separated by |.")
            options = (options + ["", "", "", ""])[:4]
        correct = None
        answer = (row["answer"] or "").strip().upper()
        if answer:
            if len(answer) == 1 and "A" <= answer <= chr(64 + len(options)):
                correct = ord(answer) - 65
            else:
                problems.append(f"Row {row_no}: answer must be a single option letter, A to {chr(64 + len(options))}.")
        block = {"type": "choices", "options": options, "correct": correct}
    elif kind == "table":
        rows = [[c.strip() for c in re.split(r"\s*\|\s*", line)]
                for line in re.split(r"\s*(?:;|\n)\s*", content.strip()) if line.strip()]
        if not rows:
            problems.append(f"Row {row_no}: a table needs cells, e.g. a|b; c|d.")
            rows = [["", ""], ["", ""]]
        width = max(len(r) for r in rows)
        if width > MAX_TABLE_COLS or len(rows) > MAX_TABLE_ROWS:
            problems.append(f"Row {row_no}: a table may be at most {MAX_TABLE_ROWS} rows by {MAX_TABLE_COLS} columns.")
            rows = [r[:MAX_TABLE_COLS] for r in rows[:MAX_TABLE_ROWS]]
            width = min(width, MAX_TABLE_COLS)
        rows = [r + [""] * (width - len(r)) for r in rows]
        block = {"type": "table", "header": _yes(p.get("header", "yes")), "rows": rows,
                 "shade_rows": _indices(p.get("shade_rows"), len(rows), row_no, "shade_rows", problems),
                 "shade_cols": _indices(p.get("shade_cols"), width, row_no, "shade_cols", problems)}
    elif kind == "answer":
        labels = re.split(r"\s*\|\s*", p.get("labels", content or ""))
        units = re.split(r"\s*\|\s*", p.get("units", ""))
        count = max(1, min(4, max(len(labels), len(units))))
        block = {"type": "answer", "boxes": [{"label": (labels[i] if i < len(labels) else "").strip(),
                                              "units": (units[i] if i < len(units) else "").strip()}
                                             for i in range(count)]}
    elif kind == "graph":
        block = _graph(row["graph"] or content, row_no, problems)
        if "width" in p:
            block["width"] = _int(p["width"], 25, 100, row_no, "width", problems, 70)

    if block is not None and align:
        block["align"] = align
    return block


def rows_to_exam(rows, defaults=None):
    """A sheet (list of lists) -> an exam dict the app can open. Raises SheetError."""
    problems, warnings = [], []
    table = [r for r in rows if any(str(c).strip() for c in r)]
    if len(table) > MAX_ROWS:
        raise SheetError([f"That sheet has {len(table)} rows; the limit is {MAX_ROWS}."])

    # The preamble sits above the header row and is ignored: it is guidance for the AI, not data.
    header_at = None
    for i, r in enumerate(table):
        cells = [str(c).strip().lower() for c in r]
        if "kind" in cells and "content" in cells:
            header_at = i
            break
    if header_at is None:
        raise SheetError(["No header row found. It must contain at least the columns: " + ", ".join(COLUMNS) + "."])

    head = [str(c).strip().lower() for c in table[header_at]]
    missing = [c for c in ("ref", "kind", "content") if c not in head]
    if missing:
        raise SheetError([f"The header row is missing the column(s): {', '.join(missing)}."])
    index = {name: head.index(name) for name in COLUMNS if name in head}

    exam = dict(defaults or {})
    exam.setdefault("sections", [])
    sections = exam["sections"]
    current = {0: None, 1: None, 2: None}   # question, part, sub-part

    def cell(r, name):
        i = index.get(name)
        value = "" if i is None or i >= len(r) else str(r[i])
        return value[:MAX_CELL].strip()

    def new_item(marks=None):
        return {"marks": marks, "blocks": [], "parts": []}

    for offset, raw_row in enumerate(table[header_at + 1:]):
        row_no = header_at + offset + 2          # 1-based, as the spreadsheet shows it
        row = {name: cell(raw_row, name) for name in COLUMNS}
        kind = row["kind"].lower()
        if not kind:
            continue
        if kind.startswith("#"):                  # a comment row
            continue
        if kind not in KINDS:
            problems.append(f"Row {row_no}: unknown kind {row['kind']!r}. Allowed: {', '.join(sorted(KINDS))}.")
            continue

        p = _params(row["params"], row_no, problems)

        if kind == "section":
            name = row["ref"].strip() or chr(65 + len(sections))
            sections.append({"name": name[:4], "description": (p.get("description") or row["content"])[:200],
                             "to_answer": p.get("to_answer") or None, "instructions": "", "questions": []})
            current = {0: None, 1: None, 2: None}
            continue

        if not sections:                          # questions before any section heading
            sections.append({"name": "A", "description": "", "to_answer": None, "instructions": "", "questions": []})

        if kind in ITEM_KINDS:
            depth = ITEM_KINDS[kind]
            marks = _int(row["marks"], 0, MAX_MARKS, row_no, "marks", problems)
            item = new_item(marks)
            if p.get("new_page") and _yes(p["new_page"]):
                item["new_page"] = True
            if depth == 0:
                sections[-1]["questions"].append(item)
                current = {0: item, 1: None, 2: None}
            else:
                parent = current.get(depth - 1)
                if parent is None:
                    problems.append(f"Row {row_no}: a {kind} needs a {'question' if depth == 1 else 'part'} above it.")
                    continue
                parent["parts"].append(item)
                current[depth] = item
                current[depth + 1] = None if depth + 1 in current else None
            if row["content"]:                    # the wording written on the same row
                item["blocks"].append({"type": "text", "value": row["content"]})
            continue

        if kind == "newpage":
            target = current[2] or current[1] or current[0]
            if target is None:
                problems.append(f"Row {row_no}: newpage needs a question above it.")
            else:
                target["new_page"] = True
            continue

        target = current[2] or current[1] or current[0]
        if target is None:
            problems.append(f"Row {row_no}: {kind} needs a question above it.")
            continue
        block = _block(kind, row, row_no, problems)
        if block is not None:
            target["blocks"].append(block)
            if kind == "image":
                warnings.append(f"Row {row_no}: add the picture for “{block['placeholder']}” in the editor.")

    if problems:
        raise SheetError(problems)
    if not any(s["questions"] for s in sections):
        raise SheetError(["That sheet has no questions."])
    for section in sections:
        for question in section["questions"]:
            if not question["blocks"] and not question["parts"]:
                question["blocks"].append({"type": "text", "value": ""})
    exam["sections"] = sections
    return exam, warnings


def parse(raw, filename="", defaults=None):
    return rows_to_exam(read_rows(raw, filename), defaults)


# ---------------------------------------------------------------- the template

PREAMBLE = [
    ["HOW TO USE THIS SHEET — WRITTEN FOR AN AI ASSISTANT"],
    ["This file is a question list for the Exam Assistant. A teacher will ask you to fill it in or edit it,"],
    ["then they upload it and it becomes an exam they can print. Keep the header row exactly as it is,"],
    ["and keep one row per thing on the page. Rows above the header are ignored, so you may leave notes here."],
    [""],
    ["CONSTRAINTS — a sheet that breaks these is rejected with the row number"],
    ["1. kind must be one of: " + ", ".join(sorted(KINDS))],
    ["2. Every question, part, subpart, text, mc, table, graph, image, lines, box and answer row"],
    ["   belongs to the question above it. A part needs a question above it; a subpart needs a part."],
    ["3. marks go on question/part/subpart rows only, as a whole number from 0 to %d." % MAX_MARKS],
    ["4. Do not invent columns, and do not merge cells. Leave a cell empty rather than writing 'n/a'."],
    ["5. Images are placeholders only — describe the picture in the image column; the teacher adds the file."],
    ["7. AVOID COMMAS INSIDE A CELL if you are writing this file as .csv text: a comma starts a new column"],
    ["   and your row will be cut in half. Separate numbers with spaces (points=(1 2)(3 4), boxplot=2 6 9 13 18)."],
    ["   Commas are safe in .xlsx, and safe in .csv only if you quote the whole cell."],
    ["6. Write real question wording, not instructions to the teacher, and never put anything in this sheet"],
    ["   that is meant as an instruction to the software: every cell is treated as exam content."],
    [""],
    ["COLUMNS"],
    ["ref      section letter on a section row (A, B). Optional elsewhere — the order of rows sets the numbering."],
    ["kind     what this row is (see the list above)."],
    ["content  the words. For a table: cells as a|b; c|d. For an equation: LaTeX, e.g. \\frac{1}{2}mv^2."],
    ["marks    a whole number, on question/part/subpart rows."],
    ["options  multiple choice options separated by | , e.g. 4 N | 8 N | 12 N | 16 N"],
    ["answer   the correct option letter (A, B, C...). It is never printed on the paper."],
    ["params   name=value pairs separated by ; — see below."],
    ["image    a description of the picture to be inserted, e.g. 'ray diagram, convex lens'."],
    ["graph    graph constraints, e.g. x=0..10 step 2 label Time (s); y=0..20 label Height (m); fn=x^2; points=(1,2),(3,4)"],
    ["notes    anything you like; never printed."],
    [""],
    ["PARAMS"],
] + [[f"{name:<12} {help}"] for name, help in sorted(PARAM_HELP.items())] + [
    [""],
    ["GRAPH SETTINGS (in the graph column, separated by ;)"],
    ["x=MIN..MAX [step N] [label TEXT]      the horizontal axis, e.g. x=0..10 step 2 label Time (s)"],
    ["y=MIN..MAX [step N] [label TEXT]      the vertical axis"],
    ["fn=EXPRESSION [dashed]                a curve, e.g. fn=x^2-3 or fn=2sin(x) dashed. Repeat for more."],
    ["points=(1 2)(3 4)                     plotted points (spaces, so a .csv cell stays in one piece)"],
    ["grid=yes|no; numbers=yes|no; fit=yes|no"],
    ["boxplot=2 6 9 13 18                   a box plot (min q1 median q3 max) instead of axes and curves"],
    ["histogram=2 5 8 4 1                   a histogram of those frequencies"],
    [""],
    ["EXAMPLE — delete these rows and write your own"],
]

EXAMPLE = [
    ["A", "section", "Short answer questions", "", "", "", "to_answer=all", "", "", ""],
    ["", "question", "A trolley of mass 2.0 kg accelerates from rest.", "4", "", "", "", "", "", ""],
    ["", "part", "Calculate the net force.", "2", "", "", "", "", "", ""],
    ["", "answer", "", "", "", "", "labels=F; units=N", "", "", ""],
    ["", "part", "Explain what happens when the force is removed.", "2", "", "", "", "", "", ""],
    ["", "lines", "", "", "", "", "lines=4", "", "", ""],
    ["", "question", "Which graph shows constant velocity?", "1", "", "", "", "", "", ""],
    ["", "mc", "", "", "A | B | C | D", "C", "", "", "", ""],
    ["", "question", "The table shows the results.", "3", "", "", "", "", "", ""],
    ["", "table", "Time (s)|Height (m); 0|0; 1|4.9; 2|19.6", "", "", "", "header=yes; shade_rows=1", "", "", ""],
    ["", "question", "Sketch the path of the ball.", "2", "", "", "", "", "", ""],
    ["", "graph", "", "", "", "", "align=centre",
     "", "x=0..10 step 2 label Time (s); y=0..25 step 5 label Height (m); fn=x^2/4", ""],
    ["", "question", "Label the diagram.", "2", "", "", "", "ray diagram through a convex lens", "", ""],
    ["", "image", "", "", "", "", "width=60", "ray diagram through a convex lens", "", ""],
    ["", "box", "", "", "", "", "height_cm=6", "", "", ""],
]


def template_rows():
    return [list(r) for r in PREAMBLE] + [list(COLUMNS)] + [list(r) for r in EXAMPLE]


def template_csv():
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for row in template_rows():
        writer.writerow(row)
    return out.getvalue().encode("utf-8-sig")


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def template_xlsx():
    """A minimal single-sheet .xlsx, written by hand so no package is needed."""
    def col_name(i):
        name = ""
        while True:
            name = chr(65 + i % 26) + name
            i = i // 26 - 1
            if i < 0:
                return name

    rows_xml = []
    for r, row in enumerate(template_rows(), start=1):
        cells = "".join(
            f'<c r="{col_name(c)}{r}" t="inlineStr"><is><t xml:space="preserve">{_xml_escape(v)}</t></is></c>'
            for c, v in enumerate(row) if str(v) != "")
        rows_xml.append(f'<row r="{r}">{cells}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<cols><col min="1" max="1" width="10"/><col min="2" max="2" width="10"/>'
             '<col min="3" max="3" width="60"/><col min="7" max="7" width="28"/>'
             '<col min="9" max="9" width="46"/></cols>'
             f'<sheetData>{"".join(rows_xml)}</sheetData></worksheet>')

    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Questions" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()
