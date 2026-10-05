# Exam Assistant

Build school exams, tests and SACs in the browser and print them as a consistent A4 booklet (or save as PDF).

Everything is edited directly on the page:

- **Cover sheet** with your school's name and logo, reading and writing times, an automatic structure-of-book table and instructions
- **Sections, questions, parts and sub-parts**, with marks totalled automatically
- **Blocks** for text (with `$maths$`), equations (visual editor or LaTeX), images, answer lines, working boxes, tables, answer boxes with units, and multiple choice (options can be text, graphs, tables or images)
- **Graphs**: gridded axes, typed functions (`x^2 - 2`, `3sin(2x)`, `1/(x-1)`), data points, lines of best fit, time series, box plots and histograms
- **A4 pages on screen** that match the printed copy, "New page" breaks, drag and drop, and undo
- **Teacher libraries** by three-letter staff code, grouped by learning area, subject and year level, with a shared faculty shelf

**[Try the live demo](https://fcl-soc.github.io/exam-builder/)**: it runs entirely in your browser and saves nothing, so print or save your exam as a PDF before closing the tab.

It runs as a small web server on one computer at school; staff open it in Chrome or Edge.
It needs **no internet connection and no packages**: just Python's standard library.

## Quick start

### Windows

1. Download this repository (green **Code** button → **Download ZIP**) and unzip it.
2. Double-click **`setup.bat`** once. It downloads a portable Python into a `python` folder.
   (If you already have Python 3.10 or newer installed, you can skip this.)
3. Double-click **`start.bat`**, then open <http://localhost:7900>.
4. Other staff use `http://<this-computer's-name>:7900`. Allow Python through Windows Firewall when asked.

### macOS or Linux

```
python3 server.py            # or: python3 server.py 8080
```

## First run: set up your school

Open **School settings** (top right):

1. **Choose an admin PIN.** Anyone with it can change the school's branding, so keep it to the people who look after the app.
   Five wrong PINs lock settings for a minute.
2. Add your **school name** and upload your **logo** (PNG, JPEG or WebP).
3. Adjust the **cover instructions** and **notice box** wording, the **default task** name ("Written Examination"),
   the printed **font and text sizes**, the **answer line gap**, and the app's **colours**.

New exams pick up these settings; existing exams keep their own copy of the cover instructions.

## Using it

- Enter your **three-letter staff code** (e.g. `ABC`) in the top bar. It identifies you; it is **not a password**.
  Exams are private to your code unless you tick **Share with faculty** in Exam settings.
- Click any text to edit it. Hover a question and use the **+** beside it, or drag blocks from the palette on the left.
- **Print / Save as PDF**: choose *Save as PDF*, paper **A4**, margins **Default**, and turn **Headers and footers off**
  (the booklet prints its own page numbers).

## Write exams with Claude

Teachers can ask Claude to write a whole exam or change a single question, and watch the changes appear in their
open editor. Run `setup-connector.bat` once on the server; teachers then install the Claude Desktop extension from the
**Use with Claude** button. See the
[teacher guide](connector/TEACHERS.md) and the [connector setup](connector/README.md). The connector is optional:
without it, Exam Assistant still needs no packages and no internet connection.

Claude follows the school's question style guide (command terms, marks, answer space, multiple choice), which
admins can change in School settings.

Any exam open in the editor also updates live when it's changed somewhere else (Claude, or another tab) and merges
with your own edits instead of overwriting them.

The exam format is documented in [docs/exam-format.md](docs/exam-format.md) and
[schema/exam.schema.json](schema/exam.schema.json), with a full example in
[examples/sample_exam.json](examples/sample_exam.json). Any tool can open an exam in the editor with a
`#data=` link.

In text, write money as `\$12.50`: a bare `$` starts maths.

## Lesson plans

**Lesson plans** (top of the exam list) keeps LEARN-framework lesson plans per staff code: class, date, topic and
the five sections, with **Copy for Compass** (paste straight into a Compass lesson plan, maths included) and
**Download Word**. Claude writes and edits them through the same connector, following the school's lesson plan guide
(Victorian Curriculum 2.0), and the page updates live as it does.

## Building an exam from a spreadsheet

On the home screen, **Import from a sheet** turns a list of questions into an exam.

1. **Download the template** (Excel or CSV). The top of the sheet explains the format — it is written as
   instructions for an AI assistant, so you can hand the file straight to one.
2. **Fill it in**, or ask an assistant to: *"fill in this template with ten Year 10 questions on electricity."*
3. **Upload it.** The exam opens in the editor and you edit it like any other.

One row per thing on the page. The `kind` column says what each row is:

| `kind` | What it makes |
|---|---|
| `section` | a new section (`ref` is its letter) |
| `question`, `part`, `subpart` | a numbered item; `marks` goes on these rows |
| `text`, `equation` | wording, or LaTeX |
| `mc` | multiple choice — `options` separated by `\|`, `answer` is the correct letter (never printed) |
| `table` | cells as `a\|b; c\|d` |
| `graph` | axes, curves, points, box plots and histograms (in the `graph` column) |
| `image` | a **placeholder**: describe the picture, then add the file in the editor |
| `lines`, `box`, `answer` | answer lines, a working box, answer boxes with units |
| `newpage` | start this question on a new page |

Anything else goes in `params` as `name=value` pairs separated by `;` — for example `align=centre`, `lines=6`,
`height_cm=8`, `header=yes`, `shade_rows=1`, `units=m s^-1`.

**Maths inside a sentence must be wrapped in `$ … $`**, or it prints exactly as typed:
write `Solve $x^2 + 4x + k = 0$ for $k$`, not `Solve x^2 + 4x + k = 0 for k`. The importer warns when a cell
looks like maths that was left unwrapped. (An `equation` row is LaTeX on its own and needs no `$`.)

A sheet that breaks the rules is **rejected with the row number**, so a mistake is never quietly turned into a
strange exam. Nothing in an uploaded sheet is treated as an instruction to the software: every cell is exam content.

> **Writing the file as `.csv` text?** A comma inside a cell starts a new column and cuts the row in half.
> Separate numbers with spaces — `points=(1 2)(3 4)`, `boxplot=2 6 9 13 18` — or use the `.xlsx` template,
> where commas are safe.

## Your data

| Path | What it is |
|---|---|
| `data/exams.db` | every exam (SQLite) |
| `data/backups/` | a copy taken each time the server starts (newest 14 kept) |
| `data/settings.json`, `data/logo.*` | your school's settings and logo |

**Back up the `data` folder.** To restore exams, stop the server and copy the newest backup over `data/exams.db`.
The `data` and `python` folders are never part of the repository, so updating the code doesn't touch them.

## Updating

If you cloned with git: stop the server, run `git pull`, start it again, and refresh open browser tabs.
If you downloaded a ZIP: download the new version and copy your `data` folder into it.

## Development

```
python3 -m unittest discover tests
```

- `server.py`: web server, exam storage, backups and school settings (standard library only)
- `importer.py`: the question-spreadsheet format — reads `.xlsx` (a zip of XML) and `.csv`, writes the template, and converts a sheet into an exam
- `static/index.html`: the whole app (editor, cover sheet, graphs, print layout)
- `static/demo.js`: the live demo. With no server behind the page (GitHub Pages, a local file, or `?demo`) it answers the app's requests from memory; on the real server it does nothing
- `.github/workflows/pages.yml`: publishes `static/` as the live demo on every push
- `connector/`: the Claude connector (MCP server; needs `connector/requirements.txt`), and its Claude Desktop extension in `connector/desktop-extension/`. `python -m unittest discover connector/tests`
- `setup-connector.bat`: installs the connector's packages into the portable Python; `start.bat` then starts it too
- `.github/workflows/connector.yml`: tests the connector, and in Chromium: `#data=` links, live updates and merging, and school mode end to end
- `static/mathlive/` and `static/Sortable.min.js`: bundled copies of [MathLive](https://github.com/arnog/mathlive) (MIT) and [SortableJS](https://github.com/SortableJS/Sortable) (MIT), so it works offline

## Licence

[MIT](LICENSE). Free to use, change and share, including in your school's own version.
