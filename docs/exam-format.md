# Exam format

How an exam is stored, for anyone (or any AI) writing one outside the editor. The exact structure is
[`schema/exam.schema.json`](../schema/exam.schema.json); a complete worked example is
[`examples/sample_exam.json`](../examples/sample_exam.json). This page covers the rules the schema can't.

## Shape

```
exam
├── cover fields: subject, unit, assessment_type, year_level, semester, year, reading_min, writing_min, calculator …
└── sections[]            "Section A: Multiple choice"
    └── questions[]       "Question 1"
        ├── blocks[]      what is printed for the question
        └── parts[]       "a." "b." …
            ├── blocks[]
            └── parts[]   "i." "ii." … (sub-parts; no deeper)
```

Numbering, the structure-of-book table on the cover, and all mark totals are automatic. Don't write
"Question 1", "a." or "(3 marks)" in any text.

## Marks

Marks go on the **deepest** level only: a question with no parts carries its own `marks`; a question with parts
carries none, and each of its parts (or sub-parts) does. The editor ignores marks on anything that has parts, and
won't print until every deepest item has marks above 0. Half marks are allowed.

## Cover fields

| Field | Values | Notes |
|---|---|---|
| `learning_area` | `English`, `Health and PE`, `Humanities`, `Languages`, `Mathematics`, `Science`, `Technologies`, `The Arts` | Groups the exam in teachers' libraries |
| `subject` | text | e.g. `Year 10 Mathematics`, `Business Management` |
| `unit` | text | Topic, printed large: `Probability`, `Unit 3 AoS 1` |
| `assessment_type` | `Exam`, `Test`, `CAT`, `SAC`, `Quiz`, `Assignment`, `Practice exam` | |
| `year_level` | `7` … `12` | as a string |
| `semester`, `year` | `"1"`/`"2"`, `"2026"` | strings |
| `reading_min`, `writing_min` | whole minutes | |
| `calculator` | `none`, `scientific`, `cas` | inserted into the cover instructions |
| `task`, `instructions` | text | **Leave these out** to use the school's own wording from School settings |

## Blocks

| `type` | Fields | Use for |
|---|---|---|
| `text` | `value` | Question text. Newlines are kept. `$...$` is inline LaTeX |
| `equation` | `value` | A displayed equation in LaTeX, **without** `$` (e.g. `\frac{a}{b}`, `\begin{aligned}…\end{aligned}`) |
| `lines` | `n` (1–30) | Ruled answer lines. How many per mark: see the question style guide |
| `box` | `height_cm` (1–25) | Blank working space. How much per mark: see the question style guide |
| `answer` | `boxes`: 1–4 of `{label, units}` | A boxed final answer: `{"label": "Total cost =", "units": "dollars"}` |
| `table` | `rows` (strings), `header`, `shade_rows`, `shade_cols` | Data, or a table students complete (leave those cells `""`) |
| `choices` | `options` (2–8), `correct` | Multiple choice. Options are lettered A, B, C… automatically |
| `graph` | see below | Axes, functions, points, box plots, histograms |

`text`, `equation`, `table` and `answer` (and graphs) also take `align`: `l`, `c` or `r`.

### Text and maths

- `$x^2 + 1$` is inline maths. Every `$` must pair up.
- **Money:** write `\$12.50`: one backslash, then the dollar sign. A bare `$` starts maths.
- Use LaTeX inside `$...$`: `$\frac{3}{4}$`, `$\sqrt{2}$`, `$\pi r^2$`, `$\text{m s}^{-1}$`, `$\le$`.
- **One backslash, not two.** The text itself is `\$25` and `\frac`. In JSON *source* (like the example file below)
  each backslash is written doubled (`"\\$25"`), but if you pass the exam to a tool, the tool's JSON encoding does
  that for you: doubling it yourself stores two backslashes, which print as a stray `\` and break the maths.

### Multiple choice

`options` are strings (with `$maths$`), or whole `graph` or `table` objects; any picture option lays the four
options out 2 × 2. `correct` is the index of the right answer (`0` = A). It is shown to the teacher in the
editor and never printed. A multiple-choice question is usually worth 1 mark; it needs no other answer space.

### Graphs

```json
{"type": "graph", "x": {"min": -5, "max": 5, "step": 1, "label": "x"}, "y": {"min": -5, "max": 5},
 "functions": [{"expr": "x^2 - 2"}, {"expr": "2x + 1", "from": 0, "to": 3, "dashed": true}],
 "points": [{"x": 1, "y": -1, "label": "A"}, {"x": 2, "y": 2, "open": true}],
 "width": 60}
```

- **Expressions** are in `x` and use the editor's own parser: `^` for powers, implied multiplication
  (`3sin(2x)`, `(x+1)(x-1)`, `2πx`), `sqrt abs sin cos tan asin acos atan ln log exp`, constants `pi` and `e`.
  `log` is base 10, `ln` is natural log. No other variables (`t`, `a` …) are understood.
- **Blank axes** for students to sketch on: give `x` and `y` and no functions.
- `fit: true` draws a least-squares line of best fit through the points; `connect: true` joins them (time series).
- `width` is a percentage of the column (25–100, default 70). `grid` and `numbers` default to on.
- Keep `(max − min) / step` at 200 or under, or the grid and numbers are dropped.

**Box plot:** `{"type": "graph", "kind": "boxplot", "x": {"min": 0, "max": 20, "step": 2, "label": "Height (cm)"},
"box": {"data": "3 5 6 7 …"}}`. Give raw `data` (quartiles computed the VCAA way, outliers beyond 1.5 IQR) or
`min`, `q1`, `median`, `q3`, `max`. `"hidden": true` prints only the scale.

**Histogram:** `{"type": "graph", "kind": "histogram", "x": {"min": 0, "max": 10, "step": 2}, "y": {"min": 0, "max": 10,
"step": 2, "label": "Frequency"}, "hist": {"start": 0, "width": 2, "counts": "2, 5, 8, 4, 1"}}`. Bars are
`[start, start+width)`, … Use `counts` or raw `data`.

## Layout

- `new_page: true` on a question or part starts it on a new printed page.
- Section `to_answer` sets "number of questions to be answered" on the cover (leave it out for all).
- Images can't be supplied in an exam file; teachers add them in the editor.

## Ids and images (exams saved in Exam Assistant)

Every section, question and part in a saved exam has an `id` (8 hex characters), added automatically and never
printed. Ids don't change when things are reordered, so tools that edit a saved exam address questions by id rather
than by number. Leave `id` out of anything new; keep it on anything you are changing.

Images are stored inside the exam (`{"type": "image", "value": "data:image/png;base64,…", "width": 60}`). Tools
that show an exam to an AI replace `value` with a short `ref` (`"img-1a2b3c4d"`); keeping the ref keeps the image.
New images can only be added in the editor.

## Opening an exam from a link

The editor imports an exam from `#data=` in its address: the exam's JSON, compressed with raw DEFLATE, then
base64url-encoded (no padding). `connector/exam_format.py` has `pack()` and `unpack()`. The exam opens as a new
exam under the teacher's staff code. In the live demo nothing is saved, so print it or save it as a PDF.
