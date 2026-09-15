# Exam Assistant

Build school exams, tests and SACs in the browser and print them as a consistent A4 booklet (or save as PDF).

Everything is edited directly on the page:

- **Cover sheet** with your school's name and logo, reading and writing times, an automatic structure-of-book table and instructions
- **Sections, questions, parts and sub-parts**, with marks totalled automatically
- **Blocks** for text (with `$maths$`), equations (visual editor or LaTeX), images, answer lines, working boxes, tables, answer boxes with units, and multiple choice (options can be text, graphs, tables or images)
- **Graphs**: gridded axes, typed functions (`x^2 - 2`, `3sin(2x)`, `1/(x-1)`), data points, lines of best fit, time series, box plots and histograms
- **A4 pages on screen** that match the printed copy, "New page" breaks, drag and drop, and undo
- **Teacher libraries** by three-letter staff code, grouped by learning area, subject and year level, with a shared faculty shelf

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
- `static/index.html`: the whole app (editor, cover sheet, graphs, print layout)
- `static/mathlive/` and `static/Sortable.min.js`: bundled copies of [MathLive](https://github.com/arnog/mathlive) (MIT) and [SortableJS](https://github.com/SortableJS/Sortable) (MIT), so it works offline

## Licence

[MIT](LICENSE). Free to use, change and share, including in your school's own version.
