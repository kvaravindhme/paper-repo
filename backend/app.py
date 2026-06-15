"""
Anomaly Bio — Shared Paper Repository (HTTP layer)
==================================================
A small collaborative reference manager for the Anomaly Bio team
(Aravindh, Armaan, Samyak).

Features
--------
* Email/password accounts, restricted to @anomalybio.com.
* Shared library of papers everyone can see, add to, and remove from.
* Per-user reading status (unread / reading / read) so nobody duplicates
  work — you can see at a glance who is reading or has read each paper.
* Annotations / notes attached to each paper, attributed to the author.
* Automatic metadata parsing from a DOI, PubMed ID, arXiv ID, or URL.
* Per-user API keys so Claude (via the MCP connector) can search, read,
  add papers, and add annotations on the user's behalf.

Pure, dependency-free logic lives in core.py (and is unit-tested there).
This module adds the FastAPI HTTP layer and the network metadata fetchers.

Stack: FastAPI + SQLite (single file, zero external services).
Run:   uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import re
from contextlib import closing
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
from core import (
    VALID_STATUSES,
    classify_identifier,
    clean as _clean,
    find_duplicate,
    hash_password,
    new_api_key,
    new_token,
    now_iso,
    verify_password,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PAPERREPO_DB", os.path.join(BASE_DIR, "paperrepo.db"))


def _resolve_frontend_dir() -> Optional[str]:
    """Find the frontend folder no matter how the host lays out the checkout.

    Tries, in order: an explicit env override, a copy next to the backend,
    the sibling ../frontend, and ./frontend relative to the current working
    directory. Returns the first that exists, else None.
    """
    candidates = [
        os.environ.get("PAPERREPO_FRONTEND"),
        os.path.join(BASE_DIR, "frontend"),
        os.path.join(os.path.dirname(BASE_DIR), "frontend"),
        os.path.join(os.getcwd(), "frontend"),
    ]
    for c in candidates:
        if c and os.path.isdir(c) and os.path.isfile(os.path.join(c, "index.html")):
            return c
    return None


FRONTEND_DIR = _resolve_frontend_dir()
print(f"[paper-repo] frontend dir resolved to: {FRONTEND_DIR}", flush=True)
ALLOWED_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("PAPERREPO_ALLOWED_DOMAINS", "anomalybio.com").split(",")
    if d.strip()
]
INVITE_CODE = os.environ.get("PAPERREPO_INVITE_CODE", "")
CONTACT_EMAIL = os.environ.get("PAPERREPO_CONTACT_EMAIL", "team@anomalybio.com")


def get_db():
    return core.get_db(DB_PATH)


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #

def user_from_row(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "api_key": row["api_key"],
        "created_at": row["created_at"],
    }


def current_user(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> dict:
    """Resolve the caller from a session bearer token OR an API key.

    Browser clients send `Authorization: Bearer <session-token>`.
    The MCP connector sends `X-API-Key: <api-key>`.
    """
    with closing(get_db()) as db:
        if x_api_key:
            row = db.execute(
                "SELECT * FROM users WHERE api_key = ?", (x_api_key,)
            ).fetchone()
            if row:
                return user_from_row(row)
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
            row = db.execute(
                """SELECT users.* FROM sessions
                   JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token = ?""",
                (token,),
            ).fetchone()
            if row:
                return user_from_row(row)
    raise HTTPException(status_code=401, detail="Not authenticated")


# --------------------------------------------------------------------------- #
# Metadata fetchers (network)
# --------------------------------------------------------------------------- #

async def fetch_crossref(doi: str) -> Optional[dict]:
    url = f"https://api.crossref.org/works/{doi}"
    headers = {"User-Agent": f"AnomalyBioPaperRepo/1.0 (mailto:{CONTACT_EMAIL})"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return None
        msg = r.json().get("message", {})
    authors = []
    for a in msg.get("author", []) or []:
        nm = " ".join(p for p in [a.get("given"), a.get("family")] if p)
        if nm:
            authors.append(nm)
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    title = msg.get("title") or []
    container = msg.get("container-title") or []
    return {
        "title": _clean(title[0] if title else None) or f"DOI {doi}",
        "authors": "; ".join(authors) or None,
        "journal": _clean(container[0] if container else None),
        "year": year,
        "doi": doi,
        "url": _clean(msg.get("URL")),
        "abstract": _clean(re.sub("<[^<]+?>", "", msg.get("abstract", "")) or None),
        "source": "crossref",
    }


async def fetch_pubmed(pmid: str) -> Optional[dict]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    params = {
        "db": "pubmed", "id": pmid, "retmode": "json",
        "tool": "AnomalyBioPaperRepo", "email": CONTACT_EMAIL,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(base, params=params)
        if r.status_code != 200:
            return None
        result = r.json().get("result", {})
    rec = result.get(pmid)
    if not rec:
        return None
    authors = "; ".join(a.get("name", "") for a in rec.get("authors", []) if a.get("name"))
    year = None
    m = re.search(r"\b(19|20)\d{2}\b", rec.get("pubdate", ""))
    if m:
        year = int(m.group())
    doi = None
    for aid in rec.get("articleids", []):
        if aid.get("idtype") == "doi":
            doi = aid.get("value")
    return {
        "title": _clean(rec.get("title")) or f"PMID {pmid}",
        "authors": authors or None,
        "journal": _clean(rec.get("fulljournalname") or rec.get("source")),
        "year": year,
        "doi": doi,
        "pmid": pmid,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "source": "pubmed",
    }


async def fetch_arxiv(arxiv_id: str) -> Optional[dict]:
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        text = r.text

    def grab(tag: str) -> Optional[str]:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else None

    title = grab("title")
    if not title:
        return None
    summary = grab("summary")
    authors = "; ".join(re.findall(r"<author>\s*<name>(.*?)</name>", text, re.DOTALL))
    year = None
    pub = grab("published")
    if pub:
        m = re.search(r"\b(19|20)\d{2}\b", pub)
        if m:
            year = int(m.group())
    return {
        "title": title,
        "authors": authors or None,
        "journal": "arXiv",
        "year": year,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "abstract": summary,
        "source": "arxiv",
    }


async def parse_identifier(identifier: str) -> dict:
    """Turn a DOI / PMID / arXiv id / URL into a metadata dict.

    Always returns a dict with at least a `title` so the caller can save
    something even when lookups fail.
    """
    info = classify_identifier(identifier)
    kind, value = info["kind"], info["value"]
    fallback = {"title": identifier or "Untitled", "url": identifier or None,
                "source": "manual"}

    if kind == "empty":
        return fallback
    if kind == "arxiv":
        data = await fetch_arxiv(value)
        if data:
            return data
    elif kind == "pmid":
        data = await fetch_pubmed(value)
        if data:
            return data
    elif kind == "doi":
        data = await fetch_crossref(value)
        if data:
            return data
    elif kind == "url" and value.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(value)
            mt = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.DOTALL | re.IGNORECASE)
            title = re.sub(r"\s+", " ", mt.group(1)).strip() if mt else value
            return {"title": title or value, "url": value, "source": "web"}
        except Exception:
            return {"title": value, "url": value, "source": "manual"}
    return fallback


# --------------------------------------------------------------------------- #
# Pydantic request models
# --------------------------------------------------------------------------- #

class SignupReq(BaseModel):
    email: str
    name: str
    password: str
    invite_code: Optional[str] = None


class LoginReq(BaseModel):
    email: str
    password: str


class AddPaperReq(BaseModel):
    identifier: Optional[str] = None
    title: Optional[str] = None
    authors: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[int] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    url: Optional[str] = None
    abstract: Optional[str] = None
    tags: Optional[str] = None
    status: Optional[str] = None


class AnnotationReq(BaseModel):
    body: str


class StatusReq(BaseModel):
    status: str


class ParseReq(BaseModel):
    identifier: str


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(title="Anomaly Bio Paper Repository")


@app.on_event("startup")
def _startup() -> None:
    core.init_db(DB_PATH)


def paper_to_dict(db, row) -> dict:
    pid = row["id"]
    statuses = db.execute(
        """SELECT u.name AS name, u.email AS email, rs.status AS status,
                  rs.updated_at AS updated_at
           FROM reading_status rs JOIN users u ON u.id = rs.user_id
           WHERE rs.paper_id = ?""",
        (pid,),
    ).fetchall()
    ann_count = db.execute(
        "SELECT COUNT(*) AS c FROM annotations WHERE paper_id = ?", (pid,)
    ).fetchone()["c"]
    adder = db.execute(
        "SELECT name FROM users WHERE id = ?", (row["added_by"],)
    ).fetchone()
    return {
        "id": pid,
        "title": row["title"],
        "authors": row["authors"],
        "journal": row["journal"],
        "year": row["year"],
        "doi": row["doi"],
        "pmid": row["pmid"],
        "url": row["url"],
        "abstract": row["abstract"],
        "tags": row["tags"],
        "source": row["source"],
        "added_by": adder["name"] if adder else None,
        "added_at": row["added_at"],
        "annotation_count": ann_count,
        "reading_status": [dict(s) for s in statuses],
    }


# ---- Auth endpoints ------------------------------------------------------- #

@app.post("/api/signup")
def signup(req: SignupReq):
    email = req.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Invalid email")
    domain = email.split("@", 1)[1]
    if ALLOWED_DOMAINS and domain not in ALLOWED_DOMAINS:
        raise HTTPException(403, f"Sign-up is restricted to: {', '.join(ALLOWED_DOMAINS)}")
    if INVITE_CODE and (req.invite_code or "") != INVITE_CODE:
        raise HTTPException(403, "Invalid invite code")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    pw_hash, salt = hash_password(req.password)
    api_key = new_api_key()
    with closing(get_db()) as db:
        try:
            cur = db.execute(
                """INSERT INTO users (email, name, pw_hash, pw_salt, api_key, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (email, req.name.strip() or email, pw_hash, salt, api_key, now_iso()),
            )
            db.commit()
        except Exception:
            raise HTTPException(409, "An account with that email already exists")
        token = new_token()
        db.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
            (token, cur.lastrowid, now_iso()),
        )
        db.commit()
    return {"token": token, "name": req.name, "email": email, "api_key": api_key}


@app.post("/api/login")
def login(req: LoginReq):
    email = req.email.strip().lower()
    with closing(get_db()) as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not verify_password(req.password, row["pw_hash"], row["pw_salt"]):
            raise HTTPException(401, "Incorrect email or password")
        token = new_token()
        db.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
            (token, row["id"], now_iso()),
        )
        db.commit()
    return {"token": token, "name": row["name"], "email": row["email"],
            "api_key": row["api_key"]}


@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        with closing(get_db()) as db:
            db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            db.commit()
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return user


@app.post("/api/regenerate-key")
def regenerate_key(user: dict = Depends(current_user)):
    key = new_api_key()
    with closing(get_db()) as db:
        db.execute("UPDATE users SET api_key = ? WHERE id = ?", (key, user["id"]))
        db.commit()
    return {"api_key": key}


# ---- Paper endpoints ------------------------------------------------------ #

@app.get("/api/papers")
def list_papers(q: Optional[str] = None, user: dict = Depends(current_user)):
    with closing(get_db()) as db:
        rows = db.execute("SELECT * FROM papers ORDER BY added_at DESC").fetchall()
        papers = [paper_to_dict(db, r) for r in rows]
    if q:
        ql = q.lower()
        papers = [
            p for p in papers
            if ql in (p["title"] or "").lower()
            or ql in (p["authors"] or "").lower()
            or ql in (p["abstract"] or "").lower()
            or ql in (p["tags"] or "").lower()
            or ql in (p["journal"] or "").lower()
        ]
    return {"papers": papers, "count": len(papers)}


@app.get("/api/papers/{paper_id}")
def get_paper(paper_id: int, user: dict = Depends(current_user)):
    with closing(get_db()) as db:
        row = db.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Paper not found")
        paper = paper_to_dict(db, row)
        anns = db.execute(
            """SELECT a.id, a.body, a.created_at, u.name AS author
               FROM annotations a LEFT JOIN users u ON u.id = a.user_id
               WHERE a.paper_id = ? ORDER BY a.created_at""",
            (paper_id,),
        ).fetchall()
    paper["annotations"] = [dict(a) for a in anns]
    return paper


async def _resolve_paper_fields(req: AddPaperReq) -> dict:
    data: dict = {}
    if req.identifier:
        data = await parse_identifier(req.identifier)
    for field in ("title", "authors", "journal", "year", "doi", "pmid",
                  "url", "abstract", "tags"):
        val = getattr(req, field)
        if val is not None:
            data[field] = val
    if not data.get("title"):
        data["title"] = req.identifier or "Untitled"
    return data


@app.post("/api/papers")
async def add_paper(req: AddPaperReq, user: dict = Depends(current_user)):
    data = await _resolve_paper_fields(req)
    with closing(get_db()) as db:
        dup = find_duplicate(db, data)
        if dup:
            paper = paper_to_dict(db, dup)
            return JSONResponse(
                status_code=200,
                content={"duplicate": True, "paper": paper,
                         "message": "This paper is already in the library."},
            )
        cur = db.execute(
            """INSERT INTO papers
               (title, authors, journal, year, doi, pmid, url, abstract,
                tags, source, added_by, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("title"), data.get("authors"), data.get("journal"),
                data.get("year"), data.get("doi"), data.get("pmid"),
                data.get("url"), data.get("abstract"), data.get("tags"),
                data.get("source", "manual"), user["id"], now_iso(),
            ),
        )
        pid = cur.lastrowid
        if req.status and req.status in VALID_STATUSES:
            db.execute(
                """INSERT INTO reading_status (paper_id, user_id, status, updated_at)
                   VALUES (?,?,?,?)""",
                (pid, user["id"], req.status, now_iso()),
            )
        db.commit()
        row = db.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone()
        paper = paper_to_dict(db, row)
    return {"duplicate": False, "paper": paper}


@app.delete("/api/papers/{paper_id}")
def delete_paper(paper_id: int, user: dict = Depends(current_user)):
    with closing(get_db()) as db:
        row = db.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Paper not found")
        db.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        db.commit()
    return {"ok": True, "deleted": paper_id}


# ---- Reading status ------------------------------------------------------- #

@app.post("/api/papers/{paper_id}/status")
def set_status(paper_id: int, req: StatusReq, user: dict = Depends(current_user)):
    if req.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    with closing(get_db()) as db:
        row = db.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Paper not found")
        if req.status == "unread":
            db.execute(
                "DELETE FROM reading_status WHERE paper_id = ? AND user_id = ?",
                (paper_id, user["id"]),
            )
        else:
            db.execute(
                """INSERT INTO reading_status (paper_id, user_id, status, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(paper_id, user_id)
                   DO UPDATE SET status = excluded.status,
                                 updated_at = excluded.updated_at""",
                (paper_id, user["id"], req.status, now_iso()),
            )
        db.commit()
    return {"ok": True, "paper_id": paper_id, "status": req.status}


# ---- Annotations ---------------------------------------------------------- #

@app.post("/api/papers/{paper_id}/annotations")
def add_annotation(paper_id: int, req: AnnotationReq, user: dict = Depends(current_user)):
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(400, "Annotation body is empty")
    with closing(get_db()) as db:
        row = db.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Paper not found")
        cur = db.execute(
            """INSERT INTO annotations (paper_id, user_id, body, created_at)
               VALUES (?,?,?,?)""",
            (paper_id, user["id"], body, now_iso()),
        )
        db.commit()
        aid = cur.lastrowid
    return {"ok": True, "id": aid, "author": user["name"], "body": body}


@app.delete("/api/annotations/{ann_id}")
def delete_annotation(ann_id: int, user: dict = Depends(current_user)):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT user_id FROM annotations WHERE id = ?", (ann_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Annotation not found")
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "You can only delete your own annotations")
        db.execute("DELETE FROM annotations WHERE id = ?", (ann_id,))
        db.commit()
    return {"ok": True, "deleted": ann_id}


# ---- Metadata parse (no save) -------------------------------------------- #

@app.post("/api/parse")
async def parse(req: ParseReq, user: dict = Depends(current_user)):
    return await parse_identifier(req.identifier)


# ---- Health & frontend ---------------------------------------------------- #

@app.get("/api/health")
def health():
    return {"ok": True, "time": now_iso()}


if FRONTEND_DIR:
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    @app.get("/")
    def index_missing():
        # The API is up but the frontend files were not found on disk.
        return JSONResponse(
            status_code=200,
            content={
                "status": "API is running, but the frontend folder was not found.",
                "hint": "Ensure the 'frontend/' directory (with index.html) is in "
                        "the repository, or set PAPERREPO_FRONTEND to its path.",
                "api_health": "/api/health",
            },
        )
