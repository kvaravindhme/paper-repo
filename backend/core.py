"""
Core logic for the Anomaly Bio paper repository.

This module deliberately has NO third-party dependencies (no FastAPI, no
httpx) so it can be imported and unit-tested with only the Python standard
library. app.py imports everything here and adds the HTTP layer + the
network metadata fetchers on top.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional

VALID_STATUSES = {"unread", "reading", "read"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    pw_hash       TEXT NOT NULL,
    pw_salt       TEXT NOT NULL,
    api_key       TEXT UNIQUE NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS papers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    authors       TEXT,
    journal       TEXT,
    year          INTEGER,
    doi           TEXT,
    pmid          TEXT,
    url           TEXT,
    abstract      TEXT,
    tags          TEXT,
    source        TEXT,
    added_by      INTEGER,
    added_at      TEXT NOT NULL,
    FOREIGN KEY (added_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id    INTEGER NOT NULL,
    user_id     INTEGER,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS reading_status (
    paper_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (paper_id, user_id),
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    with closing(get_db(db_path)) as db:
        db.executescript(SCHEMA)
        db.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- auth -----------------------------------------------------------------

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return dk.hex(), salt


def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return secrets.compare_digest(candidate, pw_hash)


def new_api_key() -> str:
    return "ak_" + secrets.token_urlsafe(32)


def new_token() -> str:
    return secrets.token_urlsafe(32)


# --- identifier handling --------------------------------------------------

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ARXIV_RE = re.compile(r"arxiv\.org/abs/([0-9]+\.[0-9]+)", re.IGNORECASE)
ARXIV_ID_RE = re.compile(r"^\s*(\d{4}\.\d{4,5})(v\d+)?\s*$")
PUBMED_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")


def classify_identifier(identifier: str) -> dict:
    """Classify a raw identifier string into a kind + extracted value.

    Returns a dict: {"kind": one of 'arxiv'|'pmid'|'doi'|'url'|'empty',
                      "value": extracted id or original string}
    Pure function — does no network I/O — so it is fully unit-testable.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return {"kind": "empty", "value": ""}

    m = ARXIV_RE.search(identifier) or ARXIV_ID_RE.match(identifier)
    if m:
        return {"kind": "arxiv", "value": m.group(1)}

    if identifier.isdigit():
        return {"kind": "pmid", "value": identifier}
    mu = PUBMED_URL_RE.search(identifier)
    if mu:
        return {"kind": "pmid", "value": mu.group(1)}

    md = DOI_RE.search(identifier)
    if md:
        return {"kind": "doi", "value": md.group(0).rstrip(".")}

    if identifier.startswith("http"):
        return {"kind": "url", "value": identifier}

    return {"kind": "url", "value": identifier}


def find_duplicate(db: sqlite3.Connection, data: dict) -> Optional[sqlite3.Row]:
    """Return an existing paper row matching the same DOI or PMID, else None."""
    for key in ("doi", "pmid"):
        v = data.get(key)
        if v:
            row = db.execute(
                f"SELECT * FROM papers WHERE {key} = ?", (str(v),)
            ).fetchone()
            if row:
                return row
    return None


def clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
