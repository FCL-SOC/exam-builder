#!/usr/bin/env node
// Exam Assistant for Claude Desktop.
//
// Claude Desktop talks to extensions over stdio (one JSON-RPC message per line). This passes each message on to
// the school's Exam Assistant connector over HTTP, with the teacher's staff code, and passes the answer back.
// No dependencies: it runs on the Node.js that ships with Claude Desktop.
//
// Settings come from the extension's install screen (manifest.json user_config), as environment variables:
//   EXAM_CONNECTOR_URL  e.g. http://8801-openai-01:7901
//   STAFF_CODE          e.g. ABC
//   OPEN_WITH           testing only: the program that opens editor links (default: the system browser)

"use strict";

const readline = require("node:readline");
const { spawn } = require("node:child_process");

const BASE = (process.env.EXAM_CONNECTOR_URL || "").trim().replace(/\/+$/, "").replace(/\/mcp$/, "");
const STAFF_CODE = (process.env.STAFF_CODE || "").trim().toUpperCase();
const TIMEOUT_MS = 60_000;

let protocolVersion = null;  // learnt from the initialize reply; sent on every later request, as MCP asks
let queue = Promise.resolve();  // answer in the order asked

const opened = new Set();  // editor links already opened in the browser this session

// The first time Claude creates or reads an exam or lesson plan, open it in the teacher's browser so they watch it
// change. Only plain http(s) links: on Windows the link goes through cmd, so nothing cmd treats as special.
function openEditor(link) {
  if (typeof link !== "string" || opened.has(link) || !/^https?:\/\/[^\s"&^|<>%]+$/.test(link)) return;
  opened.add(link);
  const [cmd, args] = process.env.OPEN_WITH ? [process.env.OPEN_WITH, [link]]
    : process.platform === "win32" ? ["cmd", ["/c", "start", "", link]]
    : process.platform === "darwin" ? ["open", [link]] : ["xdg-open", [link]];
  try {
    const child = spawn(cmd, args, { detached: true, stdio: "ignore", windowsHide: true });
    child.on("error", e => log("couldn't open the editor:", e.message));
    child.unref();
  } catch (e) {
    log("couldn't open the editor:", e.message);
  }
}

const log = (...args) => process.stderr.write(`[exam-assistant] ${args.join(" ")}\n`);
const send = message => process.stdout.write(JSON.stringify(message) + "\n");

function failure(id, text) {
  if (id === undefined || id === null) return;  // notifications get no reply
  send({ jsonrpc: "2.0", id, error: { code: -32000, message: text } });
}

async function forward(message) {
  const id = message.id;
  if (!BASE) return failure(id, "Exam Assistant isn't set up yet: the school server address is missing. Download Exam " +
    "Assistant for Claude again from the Use with Claude button in Exam Assistant, and install it.");
  const headers = { "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
  if (STAFF_CODE) headers["X-Staff-Code"] = STAFF_CODE;
  if (protocolVersion) headers["MCP-Protocol-Version"] = protocolVersion;

  let response;
  try {
    response = await fetch(`${BASE}/mcp`, {
      method: "POST", headers, body: JSON.stringify(message), signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    log("can't reach", BASE, e.message);
    return failure(id, "Can't reach Exam Assistant. It only works on the school network: check this computer is " +
      "connected at school (or on the school VPN), then try again.");
  }
  if (response.status === 202 || response.status === 204) return;  // a notification was accepted
  const text = await response.text();
  if (!response.ok) {
    log("HTTP", response.status, text.slice(0, 200));
    return failure(id, "Exam Assistant on the school server couldn't do that right now. Try again in a minute; if it " +
      `keeps happening, ask IT to check the Claude connector is running (it answered ${response.status}).`);
  }
  const replies = (response.headers.get("content-type") || "").includes("text/event-stream")
    ? text.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => JSON.parse(line.slice(5)))
    : [JSON.parse(text)];
  for (const reply of replies.flat()) {
    if (message.method === "initialize" && reply.result?.protocolVersion) protocolVersion = reply.result.protocolVersion;
    if (message.method === "tools/call") openEditor(reply.result?.structuredContent?.editor_link);
    send(reply);
  }
}

readline.createInterface({ input: process.stdin }).on("line", line => {
  if (!line.trim()) return;
  let message;
  try { message = JSON.parse(line); } catch { return log("ignored a line that isn't JSON"); }
  queue = queue.then(() => forward(message)).catch(e => { log("error", e.stack || e); failure(message.id, String(e.message || e)); });
});
process.stdin.on("end", () => queue.finally(() => process.exit(0)));
