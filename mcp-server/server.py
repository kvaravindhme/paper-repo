"""
Anomaly Bio Paper Repository — MCP connector
============================================
Exposes the shared paper library to Claude as MCP tools so any of the three
team members can search, read, add papers, set reading status, and write
annotations directly from a Claude conversation.

Each user runs this connector with their OWN API key (from the web app →
"Claude / API key"), so actions are correctly attributed to them.

Environment variables
----------------------
  PAPERREPO_URL      Base URL of the running library (e.g. https://anomaly-papers.onrender.com)
  PAPERREPO_API_KEY  Your personal API key (starts with ak_)

Run (stdio transport, which Claude Desktop / Cowork use):
  PAPERREPO_URL=... PAPERREPO_API_KEY=... python server.py
"""

from __future__ import annotations

import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("PAPERREPO_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("PAPERREPO_API_KEY", "")

mcp = FastMCP("anomaly-paper-repo")


def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError(
            "PAPERREPO_API_KEY is not set. Get your key from the library "
            "web app → 'Claude / API key' and set it in the MCP config."
        )
    return {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, headers=_headers(), timeout=30)


def _fmt_paper(p: dict, full: bool = False) -> str:
    lines = [f"[{p['id']}] {p['title']}"]
    meta = " · ".join(str(x) for x in [p.get("authors"), p.get("journal"),
                                       p.get("year")] if x)
    if meta:
        lines.append("    " + meta)
    if p.get("doi"):
        lines.append(f"    DOI: {p['doi']}")
    if p.get("url"):
        lines.append(f"    URL: {p['url']}")
    if p.get("tags"):
        lines.append(f"    Tags: {p['tags']}")
    statuses = p.get("reading_status") or []
    if statuses:
        readers = ", ".join(f"{s['name']} ({s['status']})" for s in statuses)
        lines.append(f"    Reading status: {readers}")
    else:
        lines.append("    Reading status: nobody has opened this yet")
    lines.append(f"    Added by: {p.get('added_by','—')} · "
                 f"{p.get('annotation_count',0)} annotation(s)")
    if full and p.get("abstract"):
        lines.append(f"    Abstract: {p['abstract']}")
    if full and p.get("annotations"):
        lines.append("    Notes:")
        for a in p["annotations"]:
            lines.append(f"      - {a.get('author','—')}: {a['body']}")
    return "\n".join(lines)


@mcp.tool()
def whoami() -> str:
    """Return the identity of the account this connector is acting as."""
    with _client() as c:
        r = c.get("/api/me")
        r.raise_for_status()
        u = r.json()
    return f"Acting as {u['name']} <{u['email']}> on {BASE_URL}"


@mcp.tool()
def list_papers(query: Optional[str] = None) -> str:
    """List papers in the shared library, optionally filtered by a search term.

    Args:
        query: Optional text to match against title, authors, journal, abstract, tags.
    """
    params = {"q": query} if query else {}
    with _client() as c:
        r = c.get("/api/papers", params=params)
        r.raise_for_status()
        data = r.json()
    papers = data.get("papers", [])
    if not papers:
        return "No papers found."
    header = f"{len(papers)} paper(s):\n"
    return header + "\n\n".join(_fmt_paper(p) for p in papers)


@mcp.tool()
def get_paper(paper_id: int) -> str:
    """Get full details for one paper, including abstract and all annotations.

    Args:
        paper_id: The numeric id of the paper (shown in brackets in listings).
    """
    with _client() as c:
        r = c.get(f"/api/papers/{paper_id}")
        if r.status_code == 404:
            return f"No paper with id {paper_id}."
        r.raise_for_status()
        p = r.json()
    return _fmt_paper(p, full=True)


@mcp.tool()
def add_paper(
    identifier: Optional[str] = None,
    title: Optional[str] = None,
    authors: Optional[str] = None,
    journal: Optional[str] = None,
    year: Optional[int] = None,
    doi: Optional[str] = None,
    url: Optional[str] = None,
    tags: Optional[str] = None,
    abstract: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """Add a paper to the shared library.

    The easiest path is to pass `identifier` (a DOI, PubMed ID, arXiv ID, or
    URL) and let the server auto-fill the metadata. Explicit fields override
    anything parsed. Duplicate DOIs/PMIDs are detected and not re-added.

    Args:
        identifier: DOI / PMID / arXiv id / URL to auto-parse metadata from.
        title, authors, journal, year, doi, url, tags, abstract: explicit fields.
        status: optionally set YOUR reading status now ('unread'|'reading'|'read').
    """
    body = {k: v for k, v in {
        "identifier": identifier, "title": title, "authors": authors,
        "journal": journal, "year": year, "doi": doi, "url": url,
        "tags": tags, "abstract": abstract, "status": status,
    }.items() if v is not None}
    with _client() as c:
        r = c.post("/api/papers", json=body)
        r.raise_for_status()
        data = r.json()
    p = data["paper"]
    if data.get("duplicate"):
        return f"Already in the library (not re-added):\n{_fmt_paper(p)}"
    return f"Added:\n{_fmt_paper(p)}"


@mcp.tool()
def set_reading_status(paper_id: int, status: str) -> str:
    """Set YOUR reading status for a paper so teammates can see it.

    Args:
        paper_id: The paper's numeric id.
        status: 'unread', 'reading', or 'read'.
    """
    with _client() as c:
        r = c.post(f"/api/papers/{paper_id}/status", json={"status": status})
        if r.status_code == 404:
            return f"No paper with id {paper_id}."
        r.raise_for_status()
    return f"Set paper {paper_id} to '{status}'."


@mcp.tool()
def add_annotation(paper_id: int, body: str) -> str:
    """Attach a note/annotation to a paper, attributed to you.

    Args:
        paper_id: The paper's numeric id.
        body: The note text.
    """
    with _client() as c:
        r = c.post(f"/api/papers/{paper_id}/annotations", json={"body": body})
        if r.status_code == 404:
            return f"No paper with id {paper_id}."
        r.raise_for_status()
    return f"Annotation added to paper {paper_id}."


@mcp.tool()
def remove_paper(paper_id: int) -> str:
    """Remove a paper from the shared library (affects everyone).

    Args:
        paper_id: The paper's numeric id.
    """
    with _client() as c:
        r = c.delete(f"/api/papers/{paper_id}")
        if r.status_code == 404:
            return f"No paper with id {paper_id}."
        r.raise_for_status()
    return f"Removed paper {paper_id}."


if __name__ == "__main__":
    mcp.run()
