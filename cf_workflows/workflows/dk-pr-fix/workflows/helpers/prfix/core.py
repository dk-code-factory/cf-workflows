"""Bounded JSON, immutable artifacts and transactional application bookkeeping.

This database is application state. It contains no controller records, provider
sessions, credentials or engine ownership capabilities. It lives in the mounted
cf-workflows state namespace named by CF_WORKFLOWS_STATE, never in the target
checkout.

Adapted from Agent Workflow's portable PR-fix example (MIT; see LICENSE).
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit

MAX_JSON = 1 << 20
STATE_ENV = "CF_WORKFLOWS_STATE"
STATE_MARKER = ".owned-by-cf-workflows-dk-pr-fix"
GENERATED = "workflows/.pr-fix-portable"
LEGACY_FORMAT = "portable-pr-fix-v1"
FORMAT = "cf-workflows-dk-pr-fix-v1"
ARTIFACT = re.compile(r"artifacts/[a-f0-9]{64}\.json")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text(value, label="text"):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(label + " must be a nonempty string without NUL")
    value.encode("utf-8", "strict")
    return value


def auth(value):
    if not isinstance(value, dict) or set(value) != {"request_id", "instruction", "source"}:
        raise ValueError("authorization needs request_id, exact instruction and source")
    return {key: text(value[key], "authorization " + key) for key in value}


def identity(raw):
    parsed = urlsplit(text(raw, "PR URL"))
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)", parsed.path)
    if (parsed.scheme != "https" or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.netloc)
            or parsed.query or parsed.fragment or not match
            or match[1] in {".", ".."} or match[2] in {".", ".."}):
        raise ValueError("canonical HTTPS pull request URL required")
    host, owner, name = parsed.netloc.lower(), match[1].lower(), match[2].lower()
    number = int(match[3])
    repository = "/".join((host, owner, name))
    return {"url": f"https://{repository}/pull/{number}", "host": host,
            "owner": owner, "name": name, "repository": repository, "number": number}


def bounded_json(raw, limit=MAX_JSON):
    if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > limit:
        raise ValueError("JSON exceeds byte limit")
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError("nonfinite JSON number: " + value)
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError:
        raise ValueError("JSON nesting exceeds depth limit") from None


def atomic_json(path, value):
    path = Path(path)
    data = canonical(value)
    if len(data) > 16 << 20:
        raise ValueError("artifact exceeds byte limit")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("artifact must not be a symlink")
    descriptor, name = tempfile.mkstemp(prefix=".staged-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def state_directory(root, create=False, missing_ok=False):
    """Return the mounted state namespace for this repository.

    The namespace is a separate directory outside the target checkout. Only a
    directory carrying this suite's marker is used; an unmarked one is claimed
    only while empty, so unrelated or legacy state is never adopted.
    """
    root = Path(root).resolve(strict=True)
    raw = os.environ.get(STATE_ENV, "")
    if not raw or "\0" in raw or not os.path.isabs(raw):
        raise ValueError("cf-workflows state namespace is not mounted; set " + STATE_ENV)
    path = Path(raw)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("state namespace must be an existing directory, not a symlink")
    path = path.resolve(strict=True)
    if path.is_relative_to(root) or root.is_relative_to(path):
        raise ValueError("state namespace must be disjoint from the target checkout")
    marker = path / STATE_MARKER
    if not os.path.lexists(marker):
        if missing_ok and not create:
            return None
        if not create:
            raise ValueError("application state has not been prepared")
        # A concurrent or interrupted claim of this namespace leaves only its marker or staging files.
        if any(entry.name != STATE_MARKER and not entry.name.startswith(".staged-") for entry in path.iterdir()):
            raise ValueError("refusing a nonempty state directory without this suite's marker")
        atomic_json(marker, {"format": FORMAT})
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 1024:
        raise ValueError("refusing unrelated application directory")
    if bounded_json(marker.read_bytes()) != {"format": FORMAT}:
        raise ValueError("application ownership marker differs")
    return path


def ledger_path(directory, logical_run):
    """A logical run's ledger file, keyed by a collision-resistant digest.

    User-supplied run text never forms a path, and runs that differ only by case
    stay separate on case-insensitive file systems.
    """
    return Path(directory) / "runs" / (hashlib.sha256(text(logical_run, "logical run").encode()).hexdigest() + ".sqlite")


def artifact(root, value):
    base = state_directory(root) / "artifacts"
    if base.is_symlink():
        raise ValueError("artifact directory must not be a symlink")
    path = base / (digest(value) + ".json")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != canonical(value):
            raise ValueError("immutable artifact conflict")
    else:
        atomic_json(path, value)
    return "artifacts/" + path.name


def read_artifact(root, reference):
    if not ARTIFACT.fullmatch(text(reference, "artifact reference")):
        raise ValueError("artifact is outside application-owned artifacts")
    base = state_directory(root) / "artifacts"
    if base.is_symlink():
        raise ValueError("artifact directory must not be a symlink")
    path = base / reference.split("/", 1)[1]
    if path.is_symlink():
        raise ValueError("artifact is outside application-owned artifacts")
    value = bounded_json(path.read_bytes(), 16 << 20)
    if path.name != digest(value) + ".json":
        raise ValueError("artifact identity differs")
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY, pr TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reservations(name TEXT NOT NULL, operation TEXT NOT NULL,
 amount INTEGER NOT NULL, limit_value INTEGER NOT NULL, PRIMARY KEY(name,operation));
CREATE TABLE IF NOT EXISTS deadlines(name TEXT PRIMARY KEY, value REAL NOT NULL, started REAL);
CREATE TABLE IF NOT EXISTS extensions(id TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY, task TEXT NOT NULL, fingerprint TEXT NOT NULL,
 state TEXT NOT NULL, agent TEXT NOT NULL DEFAULT '', assignment TEXT NOT NULL,
 report TEXT NOT NULL DEFAULT '', coverage TEXT NOT NULL DEFAULT '', UNIQUE(task,fingerprint));
CREATE TABLE IF NOT EXISTS submissions(claim_id TEXT NOT NULL, round INTEGER NOT NULL,
 agent TEXT NOT NULL, report TEXT NOT NULL, PRIMARY KEY(claim_id,round));
CREATE TABLE IF NOT EXISTS report_rejections(claim_id TEXT NOT NULL, round INTEGER NOT NULL,
 agent TEXT NOT NULL, report TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(claim_id,round));
CREATE TABLE IF NOT EXISTS accepted_reports(reference TEXT PRIMARY KEY, task TEXT NOT NULL,
 kind TEXT NOT NULL, fingerprint TEXT NOT NULL, agent TEXT NOT NULL, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, kind TEXT NOT NULL, input TEXT NOT NULL,
 state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS publication_intents(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
 value TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '', audience TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS publication_aliases(id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publication_events(intent TEXT NOT NULL, seq INTEGER NOT NULL,
 event TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(intent,seq));
CREATE TABLE IF NOT EXISTS publication_effects(effect TEXT PRIMARY KEY, intent TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publication_lock(singleton INTEGER PRIMARY KEY CHECK(singleton=1), intent TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS local_commits(id TEXT PRIMARY KEY,parent TEXT NOT NULL,
 head TEXT NOT NULL,tree TEXT NOT NULL,message TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS native_failures(claim_id TEXT NOT NULL, round INTEGER NOT NULL,
 agent TEXT NOT NULL, classification TEXT NOT NULL, unsafe TEXT NOT NULL,
 diagnostic TEXT NOT NULL, PRIMARY KEY(claim_id,round));
CREATE TABLE IF NOT EXISTS review_rounds(claim_id TEXT PRIMARY KEY, task TEXT NOT NULL, basis TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ci_observations(operation TEXT PRIMARY KEY,head TEXT NOT NULL,
 status TEXT NOT NULL,result TEXT NOT NULL);
"""


# Version 2 records each deadline's original start, so a narrower runtime
# ceiling can shorten an absolute window but never extend or restart it, and
# records each review claim's round basis so reviewers share one cycle per round.
# Version 3 uses the contract's publication journal states and adds the retained
# audience assessment, an append-only event history, unique effects and the lock.
SCHEMA_VERSION = 3


def _schema_version(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("ledger schema is newer than this helper; upgrade the installed workflow")
    return version


def _migrate(db):
    if _schema_version(db) >= SCHEMA_VERSION:
        return
    if "started" not in {row[1] for row in db.execute("PRAGMA table_info(deadlines)")}:
        # A version 1 window keeps its retained absolute end. Its start is
        # unknown, so only that original end bounds it.
        db.execute("ALTER TABLE deadlines ADD COLUMN started REAL")
    if "audience" not in {row[1] for row in db.execute("PRAGMA table_info(publication_intents)")}:
        db.execute("ALTER TABLE publication_intents ADD COLUMN audience TEXT NOT NULL DEFAULT ''")
    # uncertain becomes pending and completed becomes applied. A retained
    # acknowledged response or no-effect proof moves into the event history.
    # Version 2 reset a dispatched write to prepared with a no-effect proof
    # from absence. It was sent, so it also becomes pending and is never resent.
    from .publication import effect_key
    for ident, value, state, receipt in db.execute(
            "SELECT id,value,state,receipt FROM publication_intents ORDER BY rowid").fetchall():
        retained = bounded_json(receipt) if receipt else None
        if state in ("uncertain", "prepared") and retained:
            event = "response" if "dispatch_response" in retained else "no_effect"
            evidence = retained.get("dispatch_response", retained.get("no_effect"))
            db.execute("INSERT INTO publication_events(intent,seq,event,value) VALUES(?,1,?,?)",
                       (ident, event, canonical(evidence).decode()))
            db.execute("UPDATE publication_intents SET receipt='' WHERE id=?", (ident,))
        if state == "uncertain" or state == "prepared" and retained:
            db.execute("UPDATE publication_intents SET state='pending' WHERE id=?", (ident,))
            db.execute("INSERT OR IGNORE INTO publication_lock(singleton,intent) VALUES(1,?)", (ident,))
        elif state == "completed":
            db.execute("UPDATE publication_intents SET state='applied' WHERE id=?", (ident,))
            key = effect_key(bounded_json(value), retained)
            if key is not None:
                owner = db.execute("SELECT intent FROM publication_effects WHERE effect=?", (key,)).fetchone()
                if owner and owner[0] != ident:
                    raise ValueError("ledger holds duplicate publication receipts; reconcile before upgrading")
                db.execute("INSERT OR IGNORE INTO publication_effects(effect,intent) VALUES(?,?)", (key, ident))
    db.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))


class Ledger:
    def __init__(self, root, logical_run, pr=None, read_only=False):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", text(logical_run, "logical run")):
            raise ValueError("invalid logical run")
        directory = state_directory(root, create=not read_only)
        path = ledger_path(directory, logical_run)
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("ledger must not be a symlink")
        if not read_only:
            path.parent.mkdir(mode=0o700, exist_ok=True)
        self.read_only = read_only
        self.root = root
        self.logical_run = logical_run
        self.db = sqlite3.connect(path.as_uri() + ("?mode=ro" if read_only else "?mode=rwc"),
                                  uri=True, isolation_level=None, timeout=15)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=15000")
            if not read_only:
                os.chmod(path, 0o600)
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
                _schema_version(self.db)
                self.db.executescript(SCHEMA)
                with self.transaction():
                    _migrate(self.db)
                if pr is not None:
                    with self.transaction():
                        row = self.db.execute("SELECT pr FROM groups WHERE id=?", (logical_run,)).fetchone()
                        if row and row["pr"] != pr:
                            raise ValueError("logical run belongs to another PR")
                        self.db.execute("INSERT OR IGNORE INTO groups VALUES(?,?,?)",
                                        (logical_run, pr, time.time()))
            else:
                _schema_version(self.db)
            if read_only and pr is not None:
                row = self.db.execute("SELECT pr FROM groups WHERE id=?", (logical_run,)).fetchone()
                if not row or row["pr"] != pr:
                    raise ValueError("logical run binding differs")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise ValueError("read-only hook cannot write application state")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def granted(self, name):
        """Capacity added to one allowance by retained explicit continuations."""
        values = (bounded_json(row[0]) for row in self.db.execute("SELECT value FROM extensions ORDER BY rowid"))
        return sum(value["amount"] for value in values if value.get("name") == name)

    def reserve(self, name, operation, limit, amount=1):
        """Durably reserve allowance for one stable operation identity.

        The first reservation fixes the allowance's original ceiling. A later
        run or configuration can only narrow it; only an explicit continuation
        grant adds capacity. Replaying a reserved operation, including after
        response loss under changed configuration, never consumes again.
        """
        text(name, "allowance name")
        text(operation, "allowance operation")
        if type(limit) is not int or type(amount) is not int or limit < 0 or amount < 1:
            raise ValueError("invalid allowance")
        with self.transaction():
            used = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE name=?", (name,)).fetchone()[0]
            old = self.db.execute("SELECT amount FROM reservations WHERE name=? AND operation=?",
                                  (name, operation)).fetchone()
            if old:
                if old["amount"] != amount:
                    raise ValueError("allowance operation identity conflict")
                return used
            original = self.db.execute("SELECT limit_value FROM reservations WHERE name=? ORDER BY rowid LIMIT 1",
                                       (name,)).fetchone()
            ceiling = min(limit, original[0]) if original else limit
            if used + amount > ceiling + self.granted(name):
                raise ValueError("retained allowance exhausted: " + name)
            self.db.execute("INSERT INTO reservations VALUES(?,?,?,?)", (name, operation, amount, limit))
            return used + amount

    def deadline(self, name, seconds, start=None):
        """Return a retained absolute deadline, bounded by a narrower duration.

        The first call fixes the window's start and end. A later run or
        configuration never moves the end later or restarts the window; a
        smaller duration ends it earlier, measured from the original start.
        ``start`` anchors a new window at a retained instant (a pending-only
        CI extension starts where the original window ended), never later
        than now.
        """
        if type(seconds) not in {int, float} or seconds <= 0 or seconds > 86400:
            raise ValueError("deadline duration out of bounds")
        with self.transaction():
            row = self.db.execute("SELECT value,started FROM deadlines WHERE name=?", (name,)).fetchone()
            if row:
                return row["value"] if row["started"] is None else min(row["value"], row["started"] + seconds)
            if start is not None and (type(start) not in {int, float} or start > time.time()):
                raise ValueError("deadline start must be a retained past instant")
            started = time.time() if start is None else start
            self.db.execute("INSERT INTO deadlines(name,value,started) VALUES(?,?,?)",
                            (name, started + seconds, started))
            return started + seconds

    def extend(self, request_id, name, amount, authorization):
        value = {"name": text(name), "amount": amount, "authorization": auth(authorization)}
        if type(amount) is not int or amount < 1 or amount > 10:
            raise ValueError("continuation amount out of bounds")
        if request_id != value["authorization"]["request_id"]:
            raise ValueError("continuation identity differs from authorization")
        encoded = canonical(value).decode()
        with self.transaction():
            old = self.db.execute("SELECT value FROM extensions WHERE id=?", (request_id,)).fetchone()
            if old and old[0] != encoded:
                raise ValueError("continuation request identity conflict")
            self.db.execute("INSERT OR IGNORE INTO extensions VALUES(?,?)", (request_id, encoded))
        return value


def writer_lease(root, logical_run, claim, *, acquire=False, release=False):
    """Fence the one shared Git index through a whole native writing task.

    This is application bookkeeping, never a controller/session ownership
    replacement. An abandoned writer remains fenced until its original claim
    has a retained completed or verified no-effect terminal disposition.
    """
    directory = state_directory(root)
    path = directory / "workspace-writer.sqlite"
    if path.is_symlink():
        raise ValueError("workspace writer journal must not be a symlink")
    if not acquire and not release and not path.exists():
        return
    database = sqlite3.connect(path, isolation_level=None, timeout=15)
    try:
        os.chmod(path, 0o600)
        database.execute("PRAGMA synchronous=FULL")
        database.execute("CREATE TABLE IF NOT EXISTS writer(singleton INTEGER PRIMARY KEY CHECK(singleton=1),logical_run TEXT NOT NULL,claim_id TEXT NOT NULL)")
        database.execute("BEGIN IMMEDIATE")
        old = database.execute("SELECT logical_run,claim_id FROM writer WHERE singleton=1").fetchone()
        if old and old != (logical_run, claim):
            # A crash after task completion but before releasing this local
            # fence may be settled only from that exact original claim.
            with Ledger(root, old[0], read_only=True) as original:
                row = original.db.execute("SELECT state FROM claims WHERE id=?", (old[1],)).fetchone()
                terminal = row and row[0] in {"complete", "optional_failed"}
            if not terminal:
                raise ValueError("unfinished original writer owns the workspace; resume its originating execution")
            database.execute("DELETE FROM writer WHERE singleton=1")
            old = None
        if release:
            if old == (logical_run, claim):
                database.execute("DELETE FROM writer WHERE singleton=1")
        elif acquire:
            database.execute("INSERT OR IGNORE INTO writer VALUES(1,?,?)", (logical_run, claim))
        # A remaining lease here is this exact claim's own and never fences it: a lease
        # acquired before its claim row was lost is settled only by resuming that claim.
        database.commit()
    except BaseException:
        database.rollback()
        raise
    finally:
        database.close()


def verify_writer_lease(root, logical_run, claim):
    path = state_directory(root) / "workspace-writer.sqlite"
    if path.is_symlink() or not path.is_file():
        raise ValueError("original writer ownership is unavailable")
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        owner = database.execute("SELECT logical_run,claim_id FROM writer WHERE singleton=1").fetchone()
        if owner != (logical_run, claim):
            raise ValueError("original writer ownership changed")
    finally:
        database.close()
