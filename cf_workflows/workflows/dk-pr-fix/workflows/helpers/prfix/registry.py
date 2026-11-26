"""Shared cross-PR registry of named test outcomes of exact Actions attempts (#35).

The registry is application state in the mounted cf-workflows state namespace
(``registry/ci-tests.sqlite``), shared by every logical run and PR of a base
repository and keyed by that repository. It holds what CI observation read: a
named failed test (with the diagnostic lines of its failed-step logs) or a
named passed test, bound to the exact head, workflow path, job, run, job ID and
attempt. Rows are append-only; one exact attempt and test is recorded once.
A successful attempt's logs are read once per set of saved failures they could
pass (``reads``), whether or not they name one of them.

A saved row is evidence that was once read, never authority. Fail-then-pass
history (``history``) only nominates a candidate; prfix/ci.py rereads the
failure and pass of the exact attempts before any check is accepted.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
import re
import sqlite3
import time

from .core import state_directory, text

NAME = "registry/ci-tests.sqlite"
OUTCOMES = ("fail", "pass")
CONTEXT = ("workflow", "job", "test")
# Bounds of one saved failure's diagnostic lines and of one page of saved observations.
MAX_LINES = 400
MAX_LINE = 1000
MAX_PAGE = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS observations(repository TEXT NOT NULL, head TEXT NOT NULL,
 workflow TEXT NOT NULL, job TEXT NOT NULL, test TEXT NOT NULL, outcome TEXT NOT NULL,
 run_id TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, completed TEXT NOT NULL,
 lines TEXT NOT NULL, logs_sha256 TEXT NOT NULL, source TEXT NOT NULL, recorded REAL NOT NULL,
 PRIMARY KEY(repository, run_id, attempt, job_id, test, outcome));
CREATE TABLE IF NOT EXISTS reads(repository TEXT NOT NULL, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
 attempt INTEGER NOT NULL, wanted TEXT NOT NULL, recorded REAL NOT NULL,
 PRIMARY KEY(repository, run_id, attempt, job_id, wanted));
"""
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPOSITORY = re.compile(r"^[a-z0-9.-]+/[a-z0-9_.-]+/[a-z0-9_.-]+$")


def _when(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _key(row):
    return {key: row[key] for key in ("head", *CONTEXT, "outcome", "run_id", "job_id", "attempt", "completed")}


class Registry:
    """The repository-wide registry; a read-only one never creates or writes it."""

    def __init__(self, root, read_only=False):
        directory = state_directory(root, create=not read_only, missing_ok=read_only)
        self.read_only = read_only
        self.db = None
        if directory is None:
            return
        path = directory / NAME
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("CI registry must not be a symlink")
        if read_only and not path.exists():
            return
        if not read_only:
            path.parent.mkdir(mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path.as_uri() + ("?mode=ro" if read_only else "?mode=rwc"),
                                  uri=True, isolation_level=None, timeout=15)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=15000")
            if not read_only:
                os.chmod(path, 0o600)
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
                self.db.executescript(SCHEMA)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()

    def record(self, repository, source, items):
        """Save exact named outcomes once each; a read-only registry saves nothing."""
        if self.read_only or self.db is None:
            return 0
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise ValueError("CI registry requires a canonical base repository")
        rows = []
        for item in items:
            if (item.get("outcome") not in OUTCOMES or not isinstance(item.get("head"), str)
                    or not _SHA.fullmatch(item["head"]) or _when(item.get("completed")) is None
                    or type(item.get("attempt")) is not int or item["attempt"] < 1
                    or any(not str(item.get(key, "")).isdigit() for key in ("run_id", "job_id"))):
                raise ValueError("CI registry observation lacks its exact head, attempt or completion")
            lines = item.get("lines", [])
            if (not isinstance(lines, list) or len(lines) > MAX_LINES
                    or any(not isinstance(line, str) or len(line) > MAX_LINE for line in lines)):
                raise ValueError("CI registry diagnostic lines exceed their bound")
            rows.append((repository, item["head"], *(text(item.get(key), "CI registry " + key) for key in CONTEXT),
                         item["outcome"], str(item["run_id"]), str(item["job_id"]), item["attempt"],
                         item["completed"], json.dumps(sorted(set(lines))), text(item.get("logs_sha256"), "logs digest"),
                         text(source, "CI registry source"), time.time()))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            added = self.db.total_changes - before
        except BaseException:
            self.db.rollback()
            raise
        self.db.commit()
        return added

    def _rows(self, repository, where="", values=()):
        if self.db is None:
            return []
        query = "SELECT * FROM observations WHERE repository=?" + where + " ORDER BY completed, rowid"
        return [dict(row) for row in self.db.execute(query, (repository, *values))]

    def failed_at(self, repository, head):
        """Saved failures at an exact head: the successful jobs worth reading for pass evidence."""
        return self._rows(repository, " AND head=? AND outcome='fail'", (head,))

    def was_read(self, repository, run_id, job_id, attempt, wanted):
        """Whether a successful exact attempt's logs were read for this digest of saved failures."""
        if self.db is None:
            return False
        return self.db.execute("SELECT 1 FROM reads WHERE repository=? AND run_id=? AND job_id=? AND attempt=? "
                               "AND wanted=?", (repository, str(run_id), str(job_id), attempt, wanted)).fetchone() is not None

    def mark_read(self, repository, run_id, job_id, attempt, wanted):
        """Remember one completed read; a new saved failure at the head changes ``wanted`` and reads again."""
        if self.read_only or self.db is None:
            return
        self.db.execute("INSERT OR IGNORE INTO reads VALUES(?,?,?,?,?,?)",
                        (repository, str(run_id), str(job_id), attempt, text(wanted, "read digest"), time.time()))

    def history(self, repository, context, signature):
        """The earliest saved fail-then-pass of one exact context at one commit, or None.

        The failure must carry ``signature`` as one of its diagnostic lines. The
        pass is of the same workflow, job and test at the same head, from
        another exact attempt that completed later. Either may come from any
        PR or scan of the base repository.
        """
        rows = self._rows(repository, " AND workflow=? AND job=? AND test=?",
                          tuple(context[key] for key in CONTEXT))
        for failure in rows:
            if failure["outcome"] != "fail" or signature not in json.loads(failure["lines"]):
                continue
            for passed in rows:
                if (passed["outcome"] == "pass" and passed["head"] == failure["head"]
                        and _when(passed["completed"]) > _when(failure["completed"])
                        and (passed["run_id"], passed["attempt"], passed["job_id"])
                        != (failure["run_id"], failure["attempt"], failure["job_id"])):
                    return {"kind": "same_commit_fail_pass", "failure": _key(failure), "pass": _key(passed)}
        return None

    def page(self, repository, offset=0, limit=20, context=None):
        """A page of saved observations, optionally of one exact context; no GitHub read."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"saved observation page needs offset >= 0 and limit 1..{MAX_PAGE}")
        where, values = "", ()
        if context is not None:
            where, values = " AND workflow=? AND job=? AND test=?", tuple(context[key] for key in CONTEXT)
        rows = self._rows(repository, where, values)
        items = [{**_key(row), "lines": json.loads(row["lines"]), "logs_sha256": row["logs_sha256"],
                  "source": row["source"]} for row in rows[offset:offset + limit]]
        following = offset + len(items)
        return {"items": items, "total": len(rows), "next_offset": following if following < len(rows) else None}
