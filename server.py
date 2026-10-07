"""
Exam Assistant: build exams in the browser and print them to PDF.

Standard library only, so it runs on Python 3.10 or newer (on Windows, the
portable Python that setup.bat downloads) with no packages to install:

    python server.py [port]        (default 80, the standard web port: http://<computer name>/)

Teachers identify themselves with a three-letter code. It is NOT authentication:
every query is scoped to that code, and someone else's exam is a 404 rather than
a 403. data/exams.db is snapshot into data/backups/ on every startup.

School branding (name, logo, cover wording, fonts, colours) lives in
data/settings.json and data/logo.*, is edited on the School settings page, and
changes need the admin PIN chosen on first run.
"""

import hashlib
import hmac
import html
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
import urllib.request
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent

# The portable Python that setup.bat downloads is the embeddable build, whose python311._pth
# replaces the usual path setup and leaves this folder off sys.path — so "import importer"
# fails there even though the file sits right here. Put our own folder on the path first.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importer  # noqa: E402  (after the sys.path line above, on purpose)

STATIC = ROOT / "static"
DATA = ROOT / "data"
MAX_BODY = 50 * 1024 * 1024  # images are inlined as data URIs
MAX_LOGO = 2 * 1024 * 1024
MAX_SHEET = 5 * 1024 * 1024  # an uploaded question spreadsheet
BACKUP_KEEP = 14

_OWNER_RE = re.compile(r"^[A-Za-z]{3}$")
_UID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
BAD_OWNER = "Enter your three-letter staff code (for example ABC) to use your exams."

# The Claude connector (connector/), when start.bat runs it alongside: Exam Assistant offers teachers its
# Claude Desktop extension, filled in with their staff code and this server's address.
CONNECTOR_PORT = int(os.environ.get("CONNECTOR_PORT", "7901"))
EXTENSION_DIR = ROOT / "connector" / "desktop-extension"
_HOST_RE = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(:\d+)?$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("exam-assistant")

SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {table} (
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
CREATE INDEX IF NOT EXISTS idx_{table}_owner ON {table}(owner, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_{table}_shelf ON {table}(shared, learning_area);
"""
# Details shown on the exam lists come straight out of the saved exam JSON (no schema change needed).
_DETAILS = {"topic": "unit", "assessment_type": "assessment_type", "year_level": "year_level", "task": "task",
            "semester": "semester", "year": "year", "total_marks": "total_marks", "shared_edit": "shared_edit"}
# Lesson plans live in their own table, stored and versioned exactly like exams.
_PLAN_DETAILS = {"topic": "topic", "year_level": "year_level", "class_code": "class_code", "lesson_date": "lesson_date"}


def _summary(details):
    return "exam_uid AS uid, owner, learning_area, subject, title, shared, updated_at, updated_by, " + ", ".join(
        f"json_extract(body, '$.{path}') AS {alias}" for alias, path in details.items())


_BY_RE = re.compile(r"^[a-z]{0,20}$")  # who made a save: "" for the editor, "claude" for the connector


class Conflict(Exception):
    """A save based on an older version than the one stored: someone else saved in between."""

    def __init__(self, updated_at, updated_by):
        super().__init__(updated_at)
        self.updated_at, self.updated_by = updated_at, updated_by


def normalise_owner(raw):
    """'abc' -> 'ABC'. Returns '' for anything that isn't three letters."""
    code = (raw or "").strip()
    return code.upper() if _OWNER_RE.match(code) else ""


class ExamStore:
    """Exams (or, with table="lesson_plans", lesson plans) keyed by a client-minted uid, owned by a staff code."""

    def __init__(self, path, table="exams", details=None):
        self.table = table
        self._summary = _summary(details or _DETAILS)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_TEMPLATE.format(table=table))
        columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        if "updated_by" not in columns:  # databases from before the Claude connector
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
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

    def save(self, owner, uid, exam, base=None, by=""):
        """
        Upsert; returns the new updated_at. False if the uid belongs to another teacher — a colliding uid
        must never silently move an exam between teachers — unless that teacher shared it as editable: then the
        save goes in, the exam stays theirs, and how it is shared stays their choice.

        `base` is the updated_at the change was made from. If the stored exam has moved on since (another
        tab, or Claude, saved in between), nothing is written and Conflict is raised, so the caller can merge
        rather than overwrite. No base means "overwrite", which is how saves worked before.
        """
        with self._lock:
            row = self._conn.execute(f"SELECT owner, updated_at, updated_by, body FROM {self.table} WHERE exam_uid = ?",
                                     (uid,)).fetchone()
            if row is not None and row["owner"] != owner:
                stored = json.loads(row["body"])
                if not (stored.get("shared") and stored.get("shared_edit")):
                    logger.warning("Rejected save: %s does not own exam %s", owner, uid)
                    return False
                exam = {**exam, "shared": True, "shared_edit": True}
                by = by or owner.lower()  # so the owner's open editor can say who changed it
                owner = row["owner"]
            if base and row is not None and row["updated_at"] != base:
                raise Conflict(row["updated_at"], row["updated_by"])
            now = self._next_timestamp()
            self._conn.execute(
                f"""INSERT INTO {self.table} (exam_uid, owner, learning_area, subject, title, shared,
                                      body, created_at, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(exam_uid) DO UPDATE SET
                     learning_area = excluded.learning_area, subject = excluded.subject,
                     title = excluded.title, shared = excluded.shared,
                     body = excluded.body, updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
                (uid, owner, str(exam.get("learning_area") or ""), str(exam.get("subject") or ""),
                 str(exam.get("title") or ""), 1 if exam.get("shared") else 0,
                 json.dumps(exam), now, now, by),
            )
            self._conn.commit()
            return now

    def version(self, owner, uid):
        """{updated_at, updated_by} of your own or a shared exam, else None. Cheap: the open editor polls it."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT updated_at, updated_by FROM {self.table} WHERE exam_uid = ? AND (owner = ? OR shared = 1)",
                (uid, owner)).fetchone()
        return dict(row) if row else None

    def get(self, owner, uid):
        """Your own exam, or anyone's shared one. None otherwise, so the API
        never confirms that someone else's private exam exists."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._summary}, body FROM {self.table} WHERE exam_uid = ? AND (owner = ? OR shared = 1)",
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
                f"SELECT {self._summary} FROM {self.table} WHERE owner = ? ORDER BY updated_at DESC", (owner,)
            ).fetchall()
        return [dict(r) for r in rows]

    def shelf(self, learning_area=""):
        """Exams teachers have shared with their faculty."""
        sql = f"SELECT {self._summary} FROM {self.table} WHERE shared = 1"
        args = ()
        if learning_area:
            sql += " AND learning_area = ?"
            args = (learning_area,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY updated_at DESC", args).fetchall()
        return [dict(r) for r in rows]

    def delete(self, owner, uid):
        with self._lock:
            cur = self._conn.execute(f"DELETE FROM {self.table} WHERE exam_uid = ? AND owner = ?", (uid, owner))
            self._conn.commit()
        return cur.rowcount > 0

    def count(self):
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]

    def backup(self, dest):
        dst = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()


class Feedback:
    """Suggestions teachers send from the Feedback button, kept in the exam database (so the startup backup has them)."""

    MAX_TEXT = 5000

    def __init__(self, path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT "
                           "NOT NULL, owner TEXT NOT NULL, page TEXT NOT NULL, text TEXT NOT NULL)")
        self._conn.commit()
        self._lock = threading.Lock()

    def add(self, owner, page, text):
        with self._lock:
            self._conn.execute("INSERT INTO feedback (created_at, owner, page, text) VALUES (?, ?, ?, ?)",
                               (datetime.now().isoformat(timespec="seconds"), owner, page, text))
            self._conn.commit()

    def all(self):
        with self._lock:
            rows = self._conn.execute("SELECT created_at, owner, page, text FROM feedback ORDER BY id DESC").fetchall()
        return [{"created_at": c, "owner": o, "page": p, "text": t} for c, o, p, t in rows]


class ClaudeExtension:
    """
    The Claude Desktop extension, personalised per teacher: their staff code and the connector's address are
    filled in on Claude's install screen, so installing is open-the-file, click Install. Built from the files in
    connector/desktop-extension/, standard library only.
    """

    FILES = ("manifest.json", "server/index.js", "icon.png")

    def __init__(self, folder=EXTENSION_DIR, port=CONNECTOR_PORT):
        self.folder, self.port = Path(folder), port
        self._checked, self._running = 0.0, False

    def available(self):
        """Whether the connector is running on this machine (checked at most every 10 s)."""
        if not all((self.folder / f).exists() for f in self.FILES):
            return False
        if time.monotonic() - self._checked > 10:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1) as r:
                    self._running = r.status == 200
            except OSError:
                self._running = False
            self._checked = time.monotonic()
        return self._running

    def build(self, owner, host):
        """The .mcpb as bytes. `host` is the Host the teacher used to reach this server, so the connector's address
        is the one that already works from their computer."""
        match = _HOST_RE.match(host or "")
        name = match.group(1) if match else socket.gethostname()
        manifest = json.loads((self.folder / "manifest.json").read_text(encoding="utf-8"))
        manifest["user_config"]["staff_code"]["default"] = owner
        manifest["user_config"]["server_url"]["default"] = f"http://{name}:{self.port}"
        out = BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            for f in self.FILES[1:]:
                bundle.write(self.folder / f, f)
        return out.getvalue()


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

# How questions are written (command terms, marks, wording): Claude follows it. Schools can rewrite it in School
# settings; until they do, the guide that ships with the app is used, so improvements to it reach every school.
# The lesson plan guide works the same way.
def _guide(name):
    path = ROOT / "docs" / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


DEFAULT_STYLE_GUIDE = _guide("question-style-guide.md")
DEFAULT_PLAN_GUIDE = _guide("lesson-plan-guide.md")

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
    "style_guide": DEFAULT_STYLE_GUIDE,
    "plan_guide": DEFAULT_PLAN_GUIDE,
    "extra_page": False,  # a lined "extra space for responses" page at the end of every exam
}
_GUIDES = ("style_guide", "plan_guide")
FONTS = ("Calibri, Carlito, Arial, sans-serif", "Arial, Helvetica, sans-serif", "'Segoe UI', Arial, sans-serif",
         "'Times New Roman', Times, serif", "Georgia, serif", "Verdana, sans-serif")
_NUMBER_LIMITS = {"body_size": (8, 16), "school_name_size": (16, 48), "title_size": (14, 40), "line_spacing": (6, 14)}
_TEXT_LIMITS = {"school_name": 120, "default_task": 120, "instructions": 5000, "notice": 1000, "style_guide": 30000,
                "plan_guide": 30000}
_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Logos are checked by their first bytes, not just the declared type. SVG is refused: it can carry scripts.
LOGO_TYPES = {"image/png": (".png", b"\x89PNG\r\n\x1a\n"), "image/jpeg": (".jpg", b"\xff\xd8\xff"), "image/webp": (".webp", b"RIFF")}
PIN_TRIES = 5
PIN_LOCK_SECONDS = 60
# The site login (School settings) keeps students out; teachers then type their staff code as before.
LOGIN_DAYS = 14
LOGIN_COOKIE = "ea_login"


def validate_setting(key, value):
    """The cleaned value, or ValueError with a message a teacher can act on."""
    if key in _NUMBER_LIMITS:
        lo, hi = _NUMBER_LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a number from {lo} to {hi}.")
        return value
    if key == "extra_page":
        if not isinstance(value, bool):
            raise ValueError("Extra page must be on or off.")
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
        out["login_user"] = self.login_user()
        return out

    # ---- the site login: a name and a salted, hashed password, like the PIN. Off until a password is set.
    def login_user(self):
        data = self._read()
        return data.get("site_user", "") if data.get("site_pass_hash") else ""

    def set_login(self, user, password):
        """An empty password turns the login off. A new password signs everyone out."""
        with self._lock:
            data = self._read()
            if not password:
                for key in ("site_user", "site_pass_salt", "site_pass_hash"):
                    data.pop(key, None)
            else:
                if not (isinstance(user, str) and 1 <= len(user.strip()) <= 40):
                    raise ValueError("The login name must be 1 to 40 characters.")
                if not (isinstance(password, str) and 4 <= len(password) <= 64):
                    raise ValueError("The password must be 4 to 64 characters.")
                salt = secrets.token_hex(16)
                data.update(site_user=user.strip(), site_pass_salt=salt, site_pass_hash=self._hash(password, salt),
                            session_secret=secrets.token_hex(32))
            self._write(data)

    def check_login(self, user, password):
        """The name ignores case; the password doesn't."""
        data = self._read()
        if not (data.get("site_pass_hash") and isinstance(user, str) and isinstance(password, str)):
            return False
        same_user = hmac.compare_digest(user.strip().lower().encode(), data["site_user"].lower().encode())
        same_pass = hmac.compare_digest(self._hash(password, data["site_pass_salt"]), data["site_pass_hash"])
        return same_user and same_pass

    def _sign(self, expires):
        return hmac.new(bytes.fromhex(self._read().get("session_secret", "")), str(expires).encode(), "sha256").hexdigest()

    def session_cookie(self):
        """A signed "<expiry>.<signature>": nothing to store, and it survives a server restart."""
        expires = int(time.time()) + LOGIN_DAYS * 86400
        return f"{expires}.{self._sign(expires)}"

    def session_ok(self, value):
        try:
            expires, sig = value.split(".")
            expires = int(expires)
        except (AttributeError, ValueError):
            return False
        return expires > time.time() and bool(self._read().get("session_secret")) and hmac.compare_digest(sig, self._sign(expires))

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
            # A built-in guide (or an empty box) isn't stored, so the school keeps getting the latest one.
            for key in _GUIDES:
                if key in clean and clean[key].strip() in ("", DEFAULT_SETTINGS[key].strip()):
                    data.pop(key, None)
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


class LoginGuard:
    """Brute-force protection for the site login, per computer: after FREE_TRIES wrong passwords it is locked out for
    a minute, then two, four … up to an hour. Only a right password clears its count."""

    FREE_TRIES, MAX_LOCK = 5, 3600

    def __init__(self):
        self._lock = threading.Lock()
        self._ips = {}  # ip -> [wrong tries, locked until (monotonic)]

    def wait(self, ip):
        """Seconds this computer must still wait, or 0."""
        with self._lock:
            entry = self._ips.get(ip)
            return max(0, round(entry[1] - time.monotonic())) if entry else 0

    def failed(self, ip):
        with self._lock:
            if len(self._ips) > 10_000:  # ponytail: forget everyone at 10k computers; a school has far fewer
                self._ips.clear()
            entry = self._ips.setdefault(ip, [0, 0.0])
            entry[0] += 1
            if entry[0] >= self.FREE_TRIES:
                entry[1] = time.monotonic() + min(self.MAX_LOCK, 60 * 2 ** (entry[0] - self.FREE_TRIES))

    def succeeded(self, ip):
        with self._lock:
            self._ips.pop(ip, None)


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exam Assistant · Log in</title><style>
  body {{ font: 15px/1.5 "Segoe UI", Arial, sans-serif; background: #efefef; color: #222; margin: 0; }}
  header {{ background: {colour}; color: #fff; padding: 12px 20px; font-size: 18px; font-weight: 700; }}
  form {{ max-width: 340px; margin: 12vh auto 0; background: #fff; padding: 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.15); }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }} p {{ margin: 0 0 16px; color: #555; }}
  label {{ display: block; margin: 10px 0 4px; font-weight: 600; }}
  input {{ width: 100%; box-sizing: border-box; padding: 8px; font: inherit; border: 1px solid #bbb; border-radius: 4px; }}
  button {{ margin-top: 16px; width: 100%; padding: 10px; font: inherit; font-weight: 600; border: 0; border-radius: 4px;
           background: {colour}; color: #fff; cursor: pointer; }}
  .bad {{ color: #b00020; margin: 12px 0 0; }}
</style></head><body><header>Exam Assistant</header>
<form method="post" action="/login"><h1>{school}</h1><p>Staff log in</p>
  <input type="hidden" name="next" value="{next}">
  <label for="user">Login name</label><input id="user" name="user" autocomplete="username" autofocus required>
  <label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
  <button>Log in</button>{message}</form></body></html>"""


def _safe_next(path):
    """Only a path on this site: never //elsewhere.example or a backslash trick."""
    return path if path.startswith("/") and not path.startswith("//") and "\\" not in path else "/"


class Handler(SimpleHTTPRequestHandler):
    store = None     # an ExamStore, set by main() or by the tests
    settings = None  # a SchoolSettings, set by main() or by the tests
    plans = None     # an ExamStore for lesson plans (table lesson_plans), set by main() or by the tests
    feedback = None  # a Feedback, set by main() or by the tests
    guard = LoginGuard()
    trust_local = True  # requests from this computer itself (the Claude connector) skip the site login
    claude = ClaudeExtension()  # the Claude Desktop extension download

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

    def _send(self, body, content_type, **headers):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
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
            self._send(body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                       if xlsx else "text/csv; charset=utf-8", Content_Disposition=f'attachment; filename="{name}"')
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
            self._send(logo.read_bytes(), next(t for t, (ext, _) in LOGO_TYPES.items() if ext == logo.suffix),
                       X_Content_Type_Options="nosniff")
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
        elif path == "/api/settings/site-login" and method == "POST":
            body = self._read_json(4096)
            if body is not None and self._pin_ok(pin_header):
                try:
                    self.settings.set_login(body.get("user") or "", body.get("password") or "")
                    self._json({"ok": True, "login_user": self.settings.login_user()})
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
        elif path == "/api/settings/feedback" and method == "GET":
            if self._pin_ok(pin_header):
                self._json(self.feedback.all())
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

    def _claude_route(self):
        """/claude/status and /claude/exam-assistant.mcpb?owner=ABC; returns False for anything else."""
        url = urlparse(self.path)
        if url.path == "/claude/status":
            self._json({"available": self.claude.available()})
            return True
        if url.path == "/claude/exam-assistant.mcpb":
            owner = normalise_owner(parse_qs(url.query).get("owner", [""])[0])
            if not owner:
                self._json({"error": BAD_OWNER}, 400)
            elif not self.claude.available():
                self._json({"error": "The Claude connector isn't running on this server."}, 404)
            else:
                self._send(self.claude.build(owner, self.headers.get("Host")), "application/octet-stream",
                           Content_Disposition='attachment; filename="exam-assistant.mcpb"')
            return True
        return False

    # ---- the site login, in front of everything
    def _redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_page(self, nxt, message="", status=200):
        s = self.settings.public()
        body = LOGIN_PAGE.format(colour=html.escape(s["theme_colour"]), school=html.escape(s["school_name"]),
                                 next=html.escape(_safe_next(nxt)),
                                 message=f'<p class="bad">{html.escape(message)}</p>' if message else "").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _login(self, url):
        if not self.settings.login_user():
            self._redirect("/")
            return True
        if self.command != "POST":
            return self._login_page(parse_qs(url.query).get("next", ["/"])[0])
        raw = self._read_body(4096)
        if raw is None:
            return True
        form = parse_qs(raw.decode("utf-8", "replace"))
        nxt, ip = form.get("next", ["/"])[0], self.client_address[0]
        wait = self.guard.wait(ip)
        if wait:
            return self._login_page(nxt, f"Too many wrong passwords. Try again in {max(1, round(wait / 60))} minute(s).", 429)
        if self.settings.check_login(form.get("user", [""])[0], form.get("password", [""])[0]):
            self.guard.succeeded(ip)
            self._redirect(_safe_next(nxt), f"{LOGIN_COOKIE}={self.settings.session_cookie()}; Max-Age={LOGIN_DAYS * 86400}; "
                                            "Path=/; HttpOnly; SameSite=Lax")
            return True
        self.guard.failed(ip)
        logger.warning("Wrong site login from %s", ip)
        time.sleep(0.5)  # slows scripted guessing; other requests carry on (one thread each)
        return self._login_page(nxt, "That login name or password isn't right.", 401)

    def _gate(self):
        """True if the request was answered here: the login page itself, or turned away for want of a login."""
        url = urlparse(self.path)
        if url.path == "/login":
            return self._login(url)
        if url.path == "/logout":
            self._redirect("/login", f"{LOGIN_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
            return True
        if not self.settings.login_user() or (self.trust_local and self.client_address[0] in ("127.0.0.1", "::1")):
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            cookie = {}
        if LOGIN_COOKIE in cookie and self.settings.session_ok(cookie[LOGIN_COOKIE].value):
            return False
        if self.command == "GET" and not url.path.startswith(("/api/", "/claude/", "/import-template", "/school-logo")):
            self._redirect("/login?next=" + quote(url.path + (f"?{url.query}" if url.query else ""), safe=""))
        else:
            self._json({"error": "Log in to Exam Assistant first: reload the page."}, 401)
        return True

    def do_HEAD(self):
        if not self._gate():
            super().do_HEAD()

    def do_GET(self):
        if self._gate():
            return
        if self._import_route("GET") or self._settings_route("GET") or self._claude_route():
            return
        api = self._api()
        if api is None:
            return super().do_GET()
        if not api:
            return
        parts, owner, query = api
        store = self._store_for(parts)
        if parts == ["shelf"]:
            return self._json(self.store.shelf(query.get("learning_area", [""])[0]))
        if store and len(parts) == 1:
            return self._json(store.list_for(owner))
        if store and len(parts) == 2:
            found = store.get(owner, parts[1])
            if found:
                return self._json(found)
        if store and len(parts) == 3 and parts[2] == "version":
            found = store.version(owner, parts[1])
            if found:
                return self._json(found)
        self._not_found()

    def _store_for(self, parts):
        """/api/exams/… → the exams, /api/plans/… → the lesson plans."""
        return {"exams": self.store, "plans": self.plans}.get(parts[0] if parts else "")

    def _feedback_route(self):
        """POST /api/feedback {text, page}: anyone may send a suggestion; the staff code, if given, says who."""
        url = urlparse(self.path)
        if url.path.rstrip("/") != "/api/feedback":
            return False
        body = self._read_json(16 * 1024)
        if body is None:
            return True
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > Feedback.MAX_TEXT:
            self._json({"error": f"Write your suggestion (up to {Feedback.MAX_TEXT} characters)."}, 400)
            return True
        owner = normalise_owner(parse_qs(url.query).get("owner", [""])[0])
        self.feedback.add(owner, str(body.get("page") or "")[:40], text.strip())
        self._json({"ok": True})
        return True

    def do_POST(self):
        if self._gate():
            return
        if not self._import_route("POST") and not self._settings_route("POST") and not self._feedback_route():
            self._json({"error": "Not found."}, 404)

    def do_PUT(self):
        if self._gate():
            return
        if self._settings_route("PUT"):
            return
        api = self._api()
        if not api:
            return None if api is False else self._not_found()
        parts, owner, query = api
        store = self._store_for(parts)
        if len(parts) != 2 or not store or not _UID_RE.match(parts[1]):
            return self._json({"error": "Bad exam id."}, 400)
        by = query.get("by", [""])[0]
        if not _BY_RE.match(by):
            return self._json({"error": "Bad 'by'."}, 400)
        exam = self._read_json()
        if exam is None:
            return
        try:
            saved = store.save(owner, parts[1], exam, base=query.get("base", [""])[0], by=by)
        except Conflict as c:
            return self._json({"error": "This exam was changed somewhere else since you opened it.",
                               "updated_at": c.updated_at, "updated_by": c.updated_by}, 409)
        if not saved:
            return self._not_found()
        self._json({"ok": True, "updated_at": saved})

    def do_DELETE(self):
        if self._gate():
            return
        if self._settings_route("DELETE"):
            return
        api = self._api()
        if not api:
            return None if api is False else self._not_found()
        parts, owner, _ = api
        store = self._store_for(parts)
        if len(parts) == 2 and store and store.delete(owner, parts[1]):
            return self._json({"ok": True})
        self._not_found()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    DATA.mkdir(exist_ok=True)
    Handler.store = ExamStore(DATA / "exams.db")
    Handler.plans = ExamStore(DATA / "exams.db", table="lesson_plans", details=_PLAN_DETAILS)
    Handler.feedback = Feedback(DATA / "exams.db")
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
