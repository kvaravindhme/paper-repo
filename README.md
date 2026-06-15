# Anomaly Bio — Shared Paper Library

A small, self-hosted collaborative reference manager for the three of us
(Aravindh, Armaan, Samyak). One shared library that everyone can see, add to,
and remove from — built so we stop reading the same paper without knowing.

## What it does

- **Shared library** — every paper anyone adds is visible to all three of us.
- **Reading status** — each person marks a paper *unread / reading / read*.
  Every paper shows who's reading it and who's finished, so you can tell at a
  glance before you start reading something a teammate already covered.
- **Add by identifier** — paste a DOI, PubMed ID, arXiv ID, or URL and the
  title, authors, journal, year, and abstract are filled in automatically
  (via Crossref / PubMed / arXiv). Duplicate DOIs/PMIDs are detected and not
  re-added.
- **Annotations** — attach notes to any paper, attributed to whoever wrote them.
- **Accounts** — email + password, restricted to `@anomalybio.com`.
- **Claude integration** — each person gets a personal API key. The included
  MCP connector lets Claude search the library, read papers, add new ones, set
  reading status, and write annotations on your behalf — directly from a Claude
  conversation.

## Layout

```
paper-repo/
├── backend/        FastAPI + SQLite API (the server)
│   ├── app.py          HTTP routes + metadata fetchers
│   ├── core.py         dependency-free logic (schema, auth, parsing)
│   ├── test_core.py    offline test suite  (python3 test_core.py)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── run_local.sh
├── frontend/       Single-page web UI (index.html, no build step)
├── mcp-server/     Claude MCP connector (server.py)
└── render.yaml     One-click cloud deploy config
```

---

## 1. Run it locally (quick test)

```bash
cd paper-repo/backend
bash run_local.sh
```

Then open **http://localhost:8000** in a browser. (Python 3.10+ required.)
The database is a single file, `backend/paperrepo.db`.

---

## 2. Host it online (so all three can reach it)

The repo is collaborative only if it's reachable by everyone, so host it once.
The free **Render** path below is the simplest; any host that runs a Python web
service works.

### Option A — Render (recommended, free tier)

1. Push this `paper-repo/` folder to a private GitHub repo.
2. Go to <https://render.com> → **New +** → **Blueprint** → connect that repo.
   Render reads `render.yaml` and provisions the service plus a 1 GB persistent
   disk for the database.
3. When it finishes you'll get a URL like
   `https://anomaly-paper-repo.onrender.com`. Share that with the team.

> Note: the free tier sleeps after inactivity, so the first request after idle
> takes ~30 s to wake. Fine for a 3-person tool. Bump to a paid instance if that
> annoys you.

### Option B — Docker (any VPS / internal box)

```bash
cd paper-repo/backend
docker build -t anomaly-paper-repo .
docker run -d -p 8000:8000 -v $PWD/data:/var/data \
  -e PAPERREPO_DB=/var/data/paperrepo.db anomaly-paper-repo
```

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PAPERREPO_DB` | `backend/paperrepo.db` | SQLite file path (use a persistent disk in the cloud) |
| `PAPERREPO_ALLOWED_DOMAINS` | `anomalybio.com` | Comma-separated email domains allowed to sign up |
| `PAPERREPO_INVITE_CODE` | *(empty)* | If set, signups also require this shared code |
| `PAPERREPO_CONTACT_EMAIL` | `team@anomalybio.com` | Sent to Crossref/PubMed as a courtesy |

For a private 3-person tool, setting `PAPERREPO_INVITE_CODE` to a shared secret
is a good extra lock on top of the email-domain restriction.

---

## 3. Set up accounts (Aravindh, Armaan, Samyak)

Once the URL is live, each person:

1. Opens the URL.
2. Clicks **"Create one"**.
3. Enters name, their `@anomalybio.com` email, and a password (8+ chars).
   (Enter the invite code too, if you set one.)
4. That's it — you're in the shared library. Everyone sees the same papers.

There's nothing to provision per-user; accounts self-serve within the allowed
domain.

---

## 4. Connect Claude (MCP connector)

Each person connects Claude with **their own** API key, so anything Claude does
(adding papers, notes, status) is attributed to the right person.

### Get your API key

In the web app, click **"Claude / API key"** (top right) → copy the key
(starts with `ak_`).

### Install the connector

The connector is a small Python program (`mcp-server/server.py`). On each
person's machine:

```bash
cd paper-repo/mcp-server
pip install -r requirements.txt
```

### Point Claude at it

Add this to your Claude MCP config (in Claude Desktop / Cowork: Settings →
Connectors / Developer → edit config), replacing the path, URL, and key:

```json
{
  "mcpServers": {
    "anomaly-papers": {
      "command": "python3",
      "args": ["/full/path/to/paper-repo/mcp-server/server.py"],
      "env": {
        "PAPERREPO_URL": "https://anomaly-paper-repo.onrender.com",
        "PAPERREPO_API_KEY": "ak_your_personal_key_here"
      }
    }
  }
}
```

Restart Claude. You can now say things like:

- *"What's in our paper library on CRISPR base editing?"*
- *"Add doi 10.1038/s41586-020-2649-2 to the library and mark me as reading it."*
- *"Has anyone read paper 12 yet? Add a note summarizing its main finding."*
- *"List papers nobody has opened yet."*

### Connector tools Claude gets

`list_papers`, `get_paper`, `add_paper`, `set_reading_status`,
`add_annotation`, `remove_paper`, `whoami`.

---

## Security notes

- Passwords are stored hashed (PBKDF2-HMAC-SHA256, 200k iterations + per-user salt).
- Sign-up is limited to the `@anomalybio.com` domain; add an invite code for more.
- API keys act as you — keep them private. You can rotate yours anytime via
  **"Claude / API key" → Regenerate** (the old key, and any connector using it,
  stops working immediately).
- This is a trusted-small-team tool. If you ever expose it more widely, put it
  behind HTTPS (Render does this automatically) and consider rate limiting.

## Tests

```bash
cd paper-repo/backend
python3 test_core.py     # 22 checks, stdlib only — no install needed
```
