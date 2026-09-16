"""
Exam Assistant: build exams in the browser and print them to PDF.

Standard library only, so it runs on Python 3.10 or newer (on Windows, the
portable Python that setup.bat downloads) with no packages to install:

    python server.py [port]        (default 7900)

Teachers identify themselves with a three-letter code. It is NOT authentication:
every query is scoped to that code, and someone else's exam is a 404 rather than
a 403. data/exams.db is snapshot into data/backups/ on every startup.

School branding (name, logo, cover wording, fonts, colours) lives in
data/settings.json and data/logo.*, is edited on the School settings page, and
changes need the admin PIN chosen on first run.
"""

import hashlib
import hmac
import importer
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
MAX_BODY = 50 * 1024 * 1024  # images are inlined as data URIs
MAX_LOGO = 2 * 1024 * 1024
MAX_SHEET = 5 * 1024 * 1024  # an uploaded question spreadsheet
BACKUP_KEEP = 14

_OWNER_RE = re.compile(r"^[A-Za-z]{3}$")
_UID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
BAD_OWNER = "Enter your three-letter staff code (for example ABC) to use your exams."

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("exam-assistant")

SCHEMA = """
CREATE TABLE IF NOT EXISTS exams (
    exam_uid      TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    learning_area TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    shared        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    body          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exams_owner ON exams(owner, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_exams_shelf ON exams(shared, learning_area);
"""
# Details shown on the exam lists come straight out of the saved exam JSON (no schema change needed).
_DETAILS = {"topic": "unit", "assessment_type": "assessment_type", "year_level": "year_level", "task": "task",
            "semester": "semester", "year": "year", "total_marks": "total_marks"}
_SUMMARY = "exam_uid AS uid, owner, learning_area, subject, title, shared, updated_at, " + ", ".join(
    f"json_extract(body, '$.{path}') AS {alias}" for alias, path in _DETAILS.items())


def normalise_owner(raw):
    """'abc' -> 'ABC'. Returns '' for anything that isn't three letters."""
    code = (raw or "").strip()
    return code.upper() if _OWNER_RE.match(code) else ""


class ExamStore:
    """Exams keyed by a client-minted uid, owned by a staff code."""

    def __init__(self, path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._last = None

    def _next_timestamp(self):
        """
        A strictly increasing ISO timestamp. Must be called holding self._lock.

        Windows' clock ticks about every 15 ms, so two autosaves can read the
        same now(); without the bump, updated_at ties and "most recent first"
        stops being true.
        """
        now = datetime.now()
        if self._last is not None and now <= self._last:
            now = self._last + timedelta(microseconds=1)
        self._last = now
        return now.isoformat(timespec="microseconds")

    def save(self, owner, uid, exam):
        """Upsert. False if the uid belongs to another teacher — a colliding uid
        must never silently move an exam between teachers."""
        with self._lock:
            row = self._conn.execute("SELECT owner FROM exams WHERE exam_uid = ?", (uid,)).fetchone()
            if row is not None and row["owner"] != owner:
                logger.warning("Rejected save: %s does not own exam %s", owner, uid)
                return False
            now = self._next_timestamp()
            self._conn.execute(
                """INSERT INTO exams (exam_uid, owner, learning_area, subject, title, shared,
                                      body, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(exam_uid) DO UPDATE SET
                     learning_area = excluded.learning_area, subject = excluded.subject,
                     title = excluded.title, shared = excluded.shared,
                     body = excluded.body, updated_at = excluded.updated_at""",
                (uid, owner, str(exam.get("learning_area") or ""), str(exam.get("subject") or ""),
                 str(exam.get("title") or ""), 1 if exam.get("shared") else 0,
                 json.dumps(exam), now, now),
            )
            self._conn.commit()
            return True

    def get(self, owner, uid):
        """Your own exam, or anyone's shared one. None otherwise, so the API
        never confirms that someone else's private exam exists."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SUMMARY}, body FROM exams WHERE exam_uid = ? AND (owner = ? OR shared = 1)",
                (uid, owner),
            ).fetchone()
        if row is None:
            return None
        found = dict(row)
        found["exam"] = json.loads(found.pop("body"))
        return found

    def list_for(self, owner):
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SUMMARY} FROM exams WHERE owner = ? ORDER BY updated_at DESC", (owner,)
            ).fetchall()
        return [dict(r) for r in rows]

    def shelf(self, learning_area=""):
        """Exams teachers have shared with their faculty."""
        sql = f"SELECT {_SUMMARY} FROM exams WHERE shared = 1"
        args = ()
        if learning_area:
            sql += " AND learning_area = ?"
            args = (learning_area,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY updated_at DESC", args).fetchall()
        return [dict(r) for r in rows]

    def delete(self, owner, uid):
        with self._lock:
            cur = self._conn.execute("DELETE FROM exams WHERE exam_uid = ? AND owner = ?", (uid, owner))
            self._conn.commit()
        return cur.rowcount > 0

    def count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0]

    def backup(self, dest):
        dst = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()


def backup_on_startup(store, backup_dir, keep=BACKUP_KEEP):
    """
    Roll a backup of the exam database. Best-effort: a failed backup must never
    stop teachers using the app. Returns the backup path, or None.

    Two deliberate guards:
    - An EMPTY database is never backed up and never triggers a prune, otherwise
      the first restart after a wiped data/ fills the rotation with empty files
      and destroys the only copies that could restore it.
    - Filenames carry microseconds, so a crash loop can't overwrite the newest
      good snapshot.
    """
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(backup_dir.glob("exams-*.db"))
        if store.count() == 0:
            if existing:
                logger.warning(
                    "Exam database is EMPTY but %d backup(s) exist - nothing was backed up or pruned. "
                    "To restore, stop the server and copy the newest backup over data/exams.db: %s",
                    len(existing), existing[-1],
                )
            return None
        dest = backup_dir / f"exams-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        if dest.exists():
            return None
        store.backup(dest)
        for stale in sorted(backup_dir.glob("exams-*.db"))[:-keep]:
            try:
                stale.unlink()
            except OSError:
                logger.warning("Could not prune old backup %s", stale.name)
        logger.info("Exams backed up -> %s", dest.name)
        return dest
    except Exception:
        logger.exception("Exam backup failed - continuing startup")
        return None


# ---------------------------------------------------------------- school settings
DEFAULT_INSTRUCTIONS = """## Instructions
Students are permitted to bring into the examination room: pens, pencils, highlighters, erasers, sharpeners and rulers.
Students are NOT permitted to bring into the examination room: blank sheets of paper, support sheets, white-out liquid/correction tape and additional resources.
{calculator}
## Materials supplied
Question and answer book.
Additional space is available at the end of the book if you need extra paper to complete an answer. Clearly label all answers with the appropriate section and question number."""

DEFAULT_SETTINGS = {
    "school_name": "Your School",
    "default_task": "Written Examination",
    "instructions": DEFAULT_INSTRUCTIONS,
    "notice": "Students are NOT permitted to bring mobile phones and/or any other unauthorised electronic devices into the examination room",
    "body_font": "Calibri, Carlito, Arial, sans-serif",
    "body_size": 11,
    "school_name_size": 33,
    "title_size": 25,
    "line_spacing": 9,
    "theme_colour": "#8B0000",
    "accent_colour": "#B8860B",
}
FONTS = ("Calibri, Carlito, Arial, sans-serif", "Arial, Helvetica, sans-serif", "'Segoe UI', Arial, sans-serif",
         "'Times New Roman', Times, serif", "Georgia, serif", "Verdana, sans-serif")
_NUMBER_LIMITS = {"body_size": (8, 16), "school_name_size": (16, 48), "title_size": (14, 40), "line_spacing": (6, 14)}
_TEXT_LIMITS = {"school_name": 120, "default_task": 120, "instructions": 5000, "notice": 1000}
_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Logos are checked by their first bytes, not just the declared type. SVG is refused: it can carry scripts.
LOGO_TYPES = {"image/png": (".png", b"\x89PNG\r\n\x1a\n"), "image/jpeg": (".jpg", b"\xff\xd8\xff"), "image/webp": (".webp", b"RIFF")}
PIN_TRIES = 5
PIN_LOCK_SECONDS = 60


def validate_setting(key, value):
    """The cleaned value, or ValueError with a message a teacher can act on."""
    if key in _NUMBER_LIMITS:
        lo, hi = _NUMBER_LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a number from {lo} to {hi}.")
        return value
    if key.endswith("_colour"):
        if not (isinstance(value, str) and _COLOUR_RE.match(value)):
            raise ValueError("Colours must look like #8B0000.")
        return value
    if key == "body_font":
        if value not in FONTS:
            raise ValueError("Choose one of the listed fonts.")
        return value
    if not isinstance(value, str) or len(value) > _TEXT_LIMITS[key]:
        raise ValueError(f"{key.replace('_', ' ').capitalize()} must be text of at most {_TEXT_LIMITS[key]} characters.")
    return value


class SchoolSettings:
    """data/settings.json (branding plus a salted hash of the admin PIN) and data/logo.*"""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.path = self.dir / "settings.json"
        self._lock = threading.Lock()
        self._fails = 0
        self._locked_until = 0.0

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def public(self):
        """Everything the page needs. Never includes the PIN hash."""
        data = self._read()
        out = {key: data.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
        out["pin_set"] = bool(data.get("pin_hash"))
        out["has_logo"] = self.logo_path() is not None
        return out

    @staticmethod
    def _hash(pin, salt):
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()

    def pin_set(self):
        return bool(self._read().get("pin_hash"))

    def check_pin(self, pin):
        """True, False, or "locked" (after PIN_TRIES wrong PINs, for PIN_LOCK_SECONDS)."""
        with self._lock:
            if time.monotonic() < self._locked_until:
                return "locked"
            data = self._read()
            ok = (bool(data.get("pin_hash")) and isinstance(pin, str) and
                  hmac.compare_digest(self._hash(pin, data["pin_salt"]), data["pin_hash"]))
            if ok:
                self._fails = 0
                return True
            self._fails += 1
            if self._fails >= PIN_TRIES:
                self._fails = 0
                self._locked_until = time.monotonic() + PIN_LOCK_SECONDS
                logger.warning("Admin PIN locked for %d seconds after %d wrong tries", PIN_LOCK_SECONDS, PIN_TRIES)
            return False

    def set_pin(self, new_pin, current=None):
        """First run: sets the PIN. Afterwards the current PIN is needed. Returns True, False or "locked"."""
        if not (isinstance(new_pin, str) and 4 <= len(new_pin) <= 64):
            raise ValueError("The PIN must be 4 to 64 characters.")
        if self.pin_set():
            result = self.check_pin(current)
            if result is not True:
                return result
        with self._lock:
            data = self._read()
            salt = secrets.token_hex(16)
            data.update(pin_salt=salt, pin_hash=self._hash(new_pin, salt))
            self._write(data)
        return True

    def update(self, changes):
        clean = {key: validate_setting(key, value) for key, value in changes.items() if key in DEFAULT_SETTINGS}
        with self._lock:
            data = self._read()
            data.update(clean)
            self._write(data)
        return self.public()

    def logo_path(self):
        for ext, _ in LOGO_TYPES.values():
            path = self.dir / f"logo{ext}"
            if path.exists():
                return path
        return None

    def save_logo(self, content_type, blob):
        if content_type not in LOGO_TYPES:
            raise ValueError("The logo must be a PNG, JPEG or WebP image.")
        ext, magic = LOGO_TYPES[content_type]
        if not blob.startswith(magic):
            raise ValueError("That file doesn't look like the image type it claims to be.")
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._remove_logos()
            (self.dir / f"logo{ext}").write_bytes(blob)

    def delete_logo(self):
        with self._lock:
            self._remove_logos()

    def _remove_logos(self):
        for ext, _ in LOGO_TYPES.values():
            (self.dir / f"logo{ext}").unlink(missing_ok=True)


class Handler(SimpleHTTPRequestHandler):
    store = None     # an ExamStore, set by main() or by the tests
    settings = None  # a SchoolSettings, set by main() or by the tests

    # Windows builds this map from the registry, which sometimes has .js as text/plain.
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
        ".woff2": "font/woff2", ".woff": "font/woff", ".svg": "image/svg+xml",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def end_headers(self):
        # Always revalidate, so a redeploy reaches every teacher on their next load.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_request(self, code="-", size="-"):
        if str(code)[:1] in ("4", "5"):  # autosave would otherwise log every few seconds
            super().log_request(code, size)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit):
        """The request body as bytes, or None after sending the error."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"error": "Bad Content-Length."}, 400)
            return None
        if length > limit:
            self._json({"error": f"That is over {limit // (1024 * 1024)} MB."}, 413)
            return None
        return self.rfile.read(length)

    def _read_json(self, limit=MAX_BODY):
        raw = self._read_body(limit)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            self._json({"error": "Expected a JSON object."}, 400)
            return None
        return value

    # ---- school settings: no staff code needed to read; the admin PIN to change
    def _pin_ok(self, pin):
        """True, or False after sending the refusal."""
        if not self.settings.pin_set():
            self._json({"error": "Set an admin PIN first."}, 409)
            return False
        result = self.settings.check_pin(pin or "")
        if result == "locked":
            self._json({"error": "Too many wrong PINs. Try again in a minute."}, 429)
            return False
        if not result:
            self._json({"error": "Wrong admin PIN."}, 403)
            return False
        return True

    def _import_route(self, method):
        """The question-sheet template and upload; returns False for anything else.

        Nothing in an uploaded sheet is obeyed. importer.parse only ever returns exam
        content, and the browser saves it through the usual PUT under the teacher's own
        code, so an import cannot reach anyone else's work.
        """
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/import-template") and method == "GET":
            xlsx = path.endswith(".xlsx")
            body = importer.template_xlsx() if xlsx else importer.template_csv()
            name = "exam-questions-template." + ("xlsx" if xlsx else "csv")
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                             if xlsx else "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        if path != "/api/import" or method != "POST":
            return False
        api = self._api()
        if not api:  # a missing or bad staff code has already been answered
            return True
        raw = self._read_body(MAX_SHEET)
        if raw is None:
            return True
        filename = (self.headers.get("X-Filename") or "").strip()[:200]
        try:
            exam, warnings = importer.parse(raw, filename)
        except importer.SheetError as e:
            self._json({"error": "That sheet could not be read.", "problems": e.problems[:50]}, 400)
        except Exception:
            logger.exception("import failed")
            self._json({"error": "That file could not be read as a spreadsheet."}, 400)
        else:
            self._json({"exam": exam, "warnings": warnings[:50]})
        return True

    def _settings_route(self, method):
        """Handles /school-logo and /api/settings/...; returns False for anything else."""
        path = urlparse(self.path).path.rstrip("/")
        if path == "/school-logo" and method == "GET":
            logo = self.settings.logo_path()
            if not logo:
                self._json({"error": "No logo uploaded."}, 404)
                return True
            body = logo.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", next(t for t, (ext, _) in LOGO_TYPES.items() if ext == logo.suffix))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return True
        if not path.startswith("/api/settings"):
            return False
        pin_header = self.headers.get("X-Admin-PIN")
        if path == "/api/settings" and method == "GET":
            self._json(self.settings.public())
        elif path == "/api/settings" and method == "PUT":
            changes = self._read_json(64 * 1024)
            if changes is not None and self._pin_ok(pin_header):
                try:
                    self._json(self.settings.update(changes))
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
        elif path == "/api/settings/unlock" and method == "POST":
            body = self._read_json(4096)
            if body is not None and self._pin_ok(body.get("pin")):
                self._json({"ok": True})
        elif path == "/api/settings/pin" and method == "POST":
            body = self._read_json(4096)
            if body is not None:
                try:
                    result = self.settings.set_pin(body.get("pin"), body.get("current"))
                except ValueError as e:
                    result = str(e)
                if result is True:
                    self._json({"ok": True})
                elif result == "locked":
                    self._json({"error": "Too many wrong PINs. Try again in a minute."}, 429)
                elif result is False:
                    self._json({"error": "The current PIN is wrong."}, 403)
                else:
                    self._json({"error": result}, 400)
        elif path == "/api/settings/logo" and method == "PUT":
            blob = self._read_body(MAX_LOGO)
            if blob is not None and self._pin_ok(pin_header):
                try:
                    self.settings.save_logo((self.headers.get("Content-Type") or "").split(";")[0].strip(), blob)
                    self._json({"ok": True})
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
        elif path == "/api/settings/logo" and method == "DELETE":
            if self._pin_ok(pin_header):
                self.settings.delete_logo()
                self._json({"ok": True})
        else:
            self._json({"error": "Not found."}, 404)
        return True

    # ---- exams: every request carries the teacher's code
    def _api(self):
        """None for a non-API path. For /api/...: (parts after 'api', owner, query),
        or False after sending the 400 for a missing/bad staff code."""
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if parts[0] != "api":
            return None
        query = parse_qs(url.query)
        owner = normalise_owner(query.get("owner", [""])[0])
        if not owner:
            self._json({"error": BAD_OWNER}, 400)
            return False
        return parts[1:], owner, query

    def _not_found(self):
        self._json({"error": "Exam not found."}, 404)

    def do_GET(self):
        if self._import_route("GET") or self._settings_route("GET"):
            return
        api = self._api()
        if api is None:
            return super().do_GET()
        if not api:
            return
        parts, owner, query = api
        if parts == ["exams"]:
            return self._json(self.store.list_for(owner))
        if parts == ["shelf"]:
            return self._json(self.store.shelf(query.get("learning_area", [""])[0]))
        if len(parts) == 2 and parts[0] == "exams":
            found = self.store.get(owner, parts[1])
            if found:
                return self._json(found)
        self._not_found()

    def do_POST(self):
        if not self._import_route("POST") and not self._settings_route("POST"):
            self._json({"error": "Not found."}, 404)

    def do_PUT(self):
        if self._settings_route("PUT"):
            return
        api = self._api()
        if not api:
            return None if api is False else self._not_found()
        parts, owner, _ = api
        if len(parts) != 2 or parts[0] != "exams" or not _UID_RE.match(parts[1]):
            return self._json({"error": "Bad exam id."}, 400)
        exam = self._read_json()
        if exam is None:
            return
        if not self.store.save(owner, parts[1], exam):
            return self._not_found()
        self._json({"ok": True})

    def do_DELETE(self):
        if self._settings_route("DELETE"):
            return
        api = self._api()
        if not api:
            return None if api is False else self._not_found()
        parts, owner, _ = api
        if len(parts) == 2 and parts[0] == "exams" and self.store.delete(owner, parts[1]):
            return self._json({"ok": True})
        self._not_found()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7900
    DATA.mkdir(exist_ok=True)
    Handler.store = ExamStore(DATA / "exams.db")
    Handler.settings = SchoolSettings(DATA)
    backup_on_startup(Handler.store, DATA / "backups")
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logger.info("Exam Assistant running: http://localhost:%d  (other staff: http://%s:%d)",
                port, socket.gethostname(), port)
    if not Handler.settings.pin_set():
        logger.info("First run: open School settings in the app to choose an admin PIN and add your school's name and logo.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
