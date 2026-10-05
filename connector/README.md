# Claude connector

Lets teachers use Claude to write and edit exams in Exam Assistant. It runs in one of two modes.

| | **School mode** (on the school network) | **Link mode** (hosted publicly) |
|---|---|---|
| Teachers use | Claude Desktop, with the extension in `desktop-extension/` | Claude on the web, desktop or phone |
| Claude can | create exams and edit any question, in the teacher's own library | write a whole exam and give a link to it |
| Teacher sees | changes appear live in their open editor | the exam when they open the link |
| Saved | yes, in Exam Assistant | only if opened on the school server |

The teacher guide is [TEACHERS.md](TEACHERS.md).

## School mode

```
Claude Desktop ──▶ extension (on each teacher's PC) ──▶ connector :7901 ──▶ Exam Assistant :7900 ──▶ exams.db
                                                                                  ▲
                                                 teacher's browser, exam open ────┘  (checks every 2 s)
```

The connector runs next to Exam Assistant and changes exams only through its web API, using the same version check
as the editor. If the teacher and Claude change an exam at the same moment, nothing is overwritten: each change is
merged question by question, and when both changed the same question the teacher's version wins.

### Set up the server (once)

1. Run `setup-connector.bat` (after `setup.bat`). It adds pip to the portable Python, installs the packages in
   `requirements.txt`, and opens port 7901 in Windows Firewall (that step needs "Run as administrator"; otherwise
   ask IT to allow inbound TCP 7901).
2. Restart `start.bat`. It now also starts the connector, minimised in its own window, and prints its address.

`start.bat` fills in these settings from the computer's name; set them as environment variables to override:

| Variable | Default from start.bat | |
|---|---|---|
| `EXAM_SERVER` | `http://127.0.0.1:7900` | Exam Assistant, as the connector reaches it |
| `EDITOR_URL` | `http://<computer name>:7900/` | Exam Assistant, as teachers' browsers reach it (in Claude's links) |
| `PUBLIC_URL` | `http://<computer name>:7901` | The connector's own address |
| `DATA_DIR` | `connector\data` | Where the version before each of Claude's changes is kept (30 days) |

Check it's running: open `http://<computer name>:7901/healthz` from a teacher's PC.

### Set up each teacher (once)

Once the connector is running, Exam Assistant shows teachers a **Use with Claude** button. It downloads the
extension with their staff code and the connector's address (the same server name they reached Exam Assistant by)
already filled in, so in Claude Desktop they only click **Install**. `server.py` builds that download from the files
in `desktop-extension/`; set `CONNECTOR_PORT` if the connector isn't on 7901.

These downloads are built per teacher, so they can't be signed, and Claude Desktop may warn that the extension is
from an unverified developer. If that's a problem, have an owner allow it (below), or sign the hand-deploy bundle
with the school's code-signing certificate (`npx @anthropic-ai/mcpb sign`) and give teachers that instead.

`desktop-extension/exam-assistant.mcpb` is the same extension without a staff code filled in, for deploying by hand.
It defaults to `http://8801-openai-01:7901`; to change that, edit `desktop-extension/manifest.json` and rebuild:

```
npx @anthropic-ai/mcpb pack connector/desktop-extension connector/desktop-extension/exam-assistant.mcpb
```

On a Team or Enterprise plan, an owner may need to allow the extension in the organisation's admin settings
first, and can deploy it to everyone ([Claude Help Centre](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)).

### Tools

| Tool | |
|---|---|
| `get_exam_format` | The format guide ([docs/exam-format.md](../docs/exam-format.md)), an example and the JSON Schema |
| `list_exams` | The teacher's exams |
| `get_exam` | A numbered outline with ids, the exam's content, and the editor link |
| `create_exam` | A whole new exam in one go |
| `edit_exam` | Changes applied all or nothing: `add`, `replace`, `remove`, `move` questions and parts; `add_section`, `update_section`; `update_details` (cover) |
| `restore_version` | Puts the exam back as it was before Claude's last change(s) |

Claude writes questions by the school's **question style guide**: command terms (as in the VCAA glossary), the
marks each usually earns, answer space per mark, multiple-choice conventions and wording. It ships as
[`docs/question-style-guide.md`](../docs/question-style-guide.md); admins can rewrite it in **School settings →
Questions written by Claude**, and Claude reads the current version each time it writes.

Content is checked with the editor's own rules (including its graph-expression parser) before anything is saved.
Only problems in what Claude changed stop a change; a teacher's own half-finished questions don't. Images stay in
the exam but are never sent to Claude: they appear as refs Claude can keep or move, and only teachers add new ones.

### Limits

- **Claude Desktop only, on the school network.** Claude's web and phone apps reach connectors from Anthropic's
  servers, which can't see the school network.
- **Staff codes aren't passwords** — the same as in the editor. Anyone can type any code in the extension, just as
  they can in the browser. Fine on the school network; don't expose port 7901 to the internet.
- **While a teacher's cursor is in a text box,** the page doesn't redraw under it; Claude's other changes appear as
  soon as they click away.

## Link mode

Run anywhere public, without `EXAM_SERVER`:

```
pip install -r connector/requirements.txt
python connector/server.py                         # http://localhost:8000/mcp
```

Tools: `get_exam_format`, `check_exam`, `create_exam_link`. The link opens the exam in the online editor (nothing is
saved; print or save as PDF) and, if `SCHOOL_URL` is set, offers "Open on the school server", which saves it. The
exam travels in the link's `#data=` part, which browsers never send to a server; the connector keeps each exam for
`LINK_DAYS` (30) so the link Claude gives is short.

Hosting: [`render.yaml`](../render.yaml) deploys it from GitHub on Render (needs a paid instance with a disk), or
`docker build -f connector/Dockerfile .` anywhere with HTTPS in front. Then add `https://<host>/mcp` in Claude as a
custom connector. Settings: `PUBLIC_URL`, `SCHOOL_URL`, `DEMO_URL`, `DATA_DIR`, `LINK_DAYS`, `PORT`,
`TRUST_PROXY` (see the top of `server.py`).

## Tests

```
python -m unittest discover tests              # Exam Assistant, including the editor's merge (needs Node.js)
python -m unittest discover connector/tests    # both modes, against a real Exam Assistant; the extension's proxy
python tests/browser_check.py [--server]       # a #data= link opens in Chromium        (these need Playwright)
python tests/live_check.py                     # live updates and merging in the editor
python tests/school_check.py                   # school mode end to end, with the teacher watching
```
