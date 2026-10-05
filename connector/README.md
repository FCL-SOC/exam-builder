# Claude connector

An MCP server that lets Claude write an exam and hand the teacher a link that opens it in Exam Assistant,
ready to edit and print. The teacher uses their own Claude account; this server only checks the exam against
the editor's rules and turns it into a link.

```
Teacher ──asks──▶ Claude ──create_exam_link──▶ this server ──▶ https://…/e/Ab3xYz09Qw1R
                                                                    │
                       ┌────────────────────────────────────────────┤
                       ▼                                            ▼
     school server  http://examserver:7900/#data=…     online editor  https://fcl-soc.github.io/exam-builder/#data=…
     (saved under the teacher's staff code)            (nothing saved: print or save as PDF)
```

The exam travels in the `#data=` part of the address, which browsers never send to a server, so it doesn't reach
GitHub. This server keeps each packed exam for 30 days so the link Claude gives is short; it stores nothing
else (no staff codes, no names, no school data).

## Tools

| Tool | What it does |
|---|---|
| `get_exam_format` | The authoring guide ([docs/exam-format.md](../docs/exam-format.md)), a complete example and the JSON Schema |
| `check_exam` | Errors (must fix) and warnings (advice), using the editor's own rules, including its graph-expression parser |
| `create_exam_link` | Checks, then returns the link, the total marks and any warnings |

There is also a `write_exam` prompt (subject, year level, topic, assessment type, marks, minutes, notes).

## Run it locally

```
pip install -r connector/requirements.txt
python connector/server.py
```

It listens on `http://localhost:8000/mcp`. To try it with Claude Desktop before hosting it, add it to
`claude_desktop_config.json` through the `mcp-remote` bridge:

```json
{"mcpServers": {"exam-assistant": {"command": "npx", "args": ["mcp-remote", "http://localhost:8000/mcp"]}}}
```

## Host it

Claude's web and mobile apps reach custom connectors from Anthropic's servers, so the connector needs a public
`https://` address. Any host that runs a Docker container with a small persistent disk works.

**Render (deploys from GitHub on every push):** New → Blueprint → pick this repository. It reads
[`render.yaml`](../render.yaml). Set `PUBLIC_URL` to the service's address once Render shows it, and optionally
`SCHOOL_URL`. The disk needs a paid instance; on a free instance every link is lost whenever it goes to sleep.

**Anywhere else:**

```
docker build -f connector/Dockerfile -t exam-connector .
docker run -d -p 8000:8000 -v exam-links:/data \
  -e PUBLIC_URL=https://exams-connector.example.org -e TRUST_PROXY=1 exam-connector
```

Put it behind HTTPS (your host's, or a reverse proxy). `TRUST_PROXY=1` makes the rate limit use the real client
address from `X-Forwarded-For`; only set it behind a proxy you control.

| Variable | Default | |
|---|---|---|
| `PUBLIC_URL` | `http://localhost:8000` | This server's public address. Used in links and the allowed `Host` header |
| `SCHOOL_URL` | (none) | Your school's Exam Assistant. Links then offer "Open on the school server", which saves into the teacher's exams. It only has to be reachable from teachers' browsers, not from this server |
| `DEMO_URL` | GitHub Pages demo | The online editor links open in |
| `DATA_DIR` | `connector/data` | Where links are kept (`links.db`) |
| `LINK_DAYS` | `30` | How long a link works |
| `PORT` | `8000` | |

## Add it to Claude

In Claude, add a custom connector (Settings → Connectors) with the URL `https://<your host>/mcp`. On a Team or
Enterprise plan an owner may need to add it for the organisation first. No sign-in is needed: the connector holds
nothing private.

Then ask, for example: *"Write a 40-mark Year 10 Mathematics test on quadratics, 50 minutes, scientific calculator."*

## Limits

- **Images** can't be included; Claude says where one should go and the teacher adds it in the editor.
- **One-way:** Claude makes a new exam each time. Edits after opening the link happen in the editor.
- **Assessment security:** exam content passes through Claude and this server. Fine for practice and
  classroom tests; follow your school's policy for SACs and exams.
- The server is open to anyone with its address. Each client is limited to 120 calls per 10 minutes, exams are
  capped at 300 KB and links expire.

## Tests

```
python -m unittest discover connector/tests          # validator, packing, tools, links, rate limit
python tests/browser_check.py [--server]             # a link really opens in Chromium (needs Playwright)
```

The validator's graph-expression parser is a port of `compileExpr` in `static/index.html`; a test runs the
editor's JavaScript with Node and fails if the two ever disagree.
