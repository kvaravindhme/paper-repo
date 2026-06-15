"""
Offline test suite for the paper-repo core logic.

Uses only the Python standard library, so it runs anywhere without installing
FastAPI/httpx. It exercises the SAME code (core.py) that the live server uses:
schema creation, password hashing, identifier classification, the full
add/dedupe/status/annotation DB workflow.

Run:  python3 test_core.py
"""

import os
import sqlite3
import tempfile
from contextlib import closing

import core

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_password():
    h, s = core.hash_password("hunter2longpw")
    check("password verifies", core.verify_password("hunter2longpw", h, s))
    check("wrong password rejected", not core.verify_password("nope", h, s))
    h2, s2 = core.hash_password("hunter2longpw")
    check("salts are unique", s != s2 and h != h2)
    check("api keys unique", core.new_api_key() != core.new_api_key())


def test_classify():
    cases = {
        "10.1038/s41586-020-2649-2": ("doi", "10.1038/s41586-020-2649-2"),
        "https://doi.org/10.1101/2021.01.01.425001": ("doi", "10.1101/2021.01.01.425001"),
        "34320281": ("pmid", "34320281"),
        "https://pubmed.ncbi.nlm.nih.gov/34320281/": ("pmid", "34320281"),
        "2103.00020": ("arxiv", "2103.00020"),
        "https://arxiv.org/abs/2103.00020": ("arxiv", "2103.00020"),
        "https://www.nature.com/articles/abc": ("url", "https://www.nature.com/articles/abc"),
        "": ("empty", ""),
    }
    for raw, (kind, val) in cases.items():
        info = core.classify_identifier(raw)
        check(f"classify {raw!r} -> {kind}",
              info["kind"] == kind and info["value"] == val)


def test_db_workflow():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        core.init_db(path)
        with closing(core.get_db(path)) as db:
            # two users
            for nm, em in [("Aravindh", "aravindh@anomalybio.com"),
                           ("Armaan", "armaan@anomalybio.com")]:
                h, s = core.hash_password("password123")
                db.execute(
                    "INSERT INTO users (email,name,pw_hash,pw_salt,api_key,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (em, nm, h, s, core.new_api_key(), core.now_iso()))
            db.commit()
            u1 = db.execute("SELECT id FROM users WHERE name='Aravindh'").fetchone()["id"]
            u2 = db.execute("SELECT id FROM users WHERE name='Armaan'").fetchone()["id"]

            # add a paper with a DOI
            data = {"title": "A great paper", "doi": "10.1000/xyz",
                    "added_by": u1}
            check("no duplicate initially", core.find_duplicate(db, data) is None)
            db.execute(
                "INSERT INTO papers (title,doi,added_by,added_at,source) "
                "VALUES (?,?,?,?,?)",
                ("A great paper", "10.1000/xyz", u1, core.now_iso(), "manual"))
            db.commit()

            # dedupe detects the same DOI
            dup = core.find_duplicate(db, {"doi": "10.1000/xyz"})
            check("duplicate DOI detected", dup is not None)
            check("different DOI not flagged",
                  core.find_duplicate(db, {"doi": "10.1000/other"}) is None)

            pid = dup["id"]

            # reading-status upsert: Aravindh reading -> read
            for st in ("reading", "read"):
                db.execute(
                    "INSERT INTO reading_status (paper_id,user_id,status,updated_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(paper_id,user_id) "
                    "DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                    (pid, u1, st, core.now_iso()))
            db.commit()
            row = db.execute(
                "SELECT status FROM reading_status WHERE paper_id=? AND user_id=?",
                (pid, u1)).fetchone()
            check("status upsert ends at 'read'", row["status"] == "read")
            cnt = db.execute(
                "SELECT COUNT(*) c FROM reading_status WHERE paper_id=? AND user_id=?",
                (pid, u1)).fetchone()["c"]
            check("upsert did not duplicate row", cnt == 1)

            # Armaan marks reading -> both visible
            db.execute(
                "INSERT INTO reading_status (paper_id,user_id,status,updated_at) "
                "VALUES (?,?,?,?)", (pid, u2, "reading", core.now_iso()))
            db.commit()
            readers = db.execute(
                "SELECT COUNT(*) c FROM reading_status WHERE paper_id=?", (pid,)
            ).fetchone()["c"]
            check("two readers tracked", readers == 2)

            # annotations
            db.execute(
                "INSERT INTO annotations (paper_id,user_id,body,created_at) "
                "VALUES (?,?,?,?)", (pid, u1, "Key result in fig 3.", core.now_iso()))
            db.commit()
            acnt = db.execute(
                "SELECT COUNT(*) c FROM annotations WHERE paper_id=?", (pid,)
            ).fetchone()["c"]
            check("annotation stored", acnt == 1)

            # cascade delete: removing the paper clears status + annotations
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("DELETE FROM papers WHERE id=?", (pid,))
            db.commit()
            left_status = db.execute(
                "SELECT COUNT(*) c FROM reading_status WHERE paper_id=?", (pid,)
            ).fetchone()["c"]
            left_ann = db.execute(
                "SELECT COUNT(*) c FROM annotations WHERE paper_id=?", (pid,)
            ).fetchone()["c"]
            check("delete cascades to reading_status", left_status == 0)
            check("delete cascades to annotations", left_ann == 0)

            # unique email enforced
            try:
                h, s = core.hash_password("password123")
                db.execute(
                    "INSERT INTO users (email,name,pw_hash,pw_salt,api_key,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    ("aravindh@anomalybio.com", "Dup", h, s,
                     core.new_api_key(), core.now_iso()))
                db.commit()
                check("duplicate email rejected", False)
            except sqlite3.IntegrityError:
                check("duplicate email rejected", True)
    finally:
        os.remove(path)


if __name__ == "__main__":
    print("Password / key tests:")
    test_password()
    print("Identifier classification tests:")
    test_classify()
    print("DB workflow tests:")
    test_db_workflow()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
