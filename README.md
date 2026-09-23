# Firstsource RFP Response Generator

A self-hosted web application that turns analyst RFIs, client questionnaires and
source documents into on-brand Firstsource deliverables: an interactive HTML
dashboard, a Q&A response document, Word, PowerPoint or Excel.

This is the standalone version of the Cowork skill of the same name. It has no
dependency on Cowork, Claude Desktop or any browser extension: a FastAPI backend
calls the Anthropic API directly with a server-held API key, and the Firstsource
knowledge base lives on the server where end users cannot see or edit it.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [File tree](#file-tree)
- [Quick start (local)](#quick-start-local)
- [Running with Docker](#running-with-docker)
- [Configuration](#configuration)
- [Single sign-on](#single-sign-on)
- [Moving to your own server](#moving-to-your-own-server)
- [Updating the knowledge base](#updating-the-knowledge-base)
- [SharePoint knowledge base](#sharepoint-knowledge-base)
- [API reference](#api-reference)
- [Tests](#tests)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Cost and performance](#cost-and-performance)
- [What changed from the Cowork skill](#what-changed-from-the-cowork-skill)

---

## What it does

1. A user uploads **one or more** files (PDF, Word, PowerPoint, Excel, text, CSV,
   TSV, JSON, Markdown) and/or pastes text.
2. The backend extracts text from every file and merges them into a single
   delimited corpus. Questions are taken from whichever document contains them;
   the rest become supporting evidence.
3. The corpus is sent to the Anthropic API together with the Firstsource
   knowledge base and the Kairos terminology rules, both held server-side.
4. The model returns a structured response document, which is validated and then
   rendered into whichever of the five formats the user selected.

**Mode detection** is automatic:

| Input | Mode | Output |
|---|---|---|
| Anything containing questions (numbered items, "describe/explain/provide", RFI or survey fields) | **RFI** | A `Responses` section answering every question, preserving the client's original numbering (`Q1`, `1.1`, `3.2(a)`) |
| A deck, report or brochure with no questions | **Summary** | An executive dashboard with one tab per theme |

**Terminology and brand** are enforced on every request: "Intelligence that
Operates" is never abbreviated, `ICF` is expanded on first mention, and the
deprecated terms `relAI`, `Agentic OS`, `UnBPO` and `ITO` are silently recast to
the current brand. Output uses the Firstsource palette, the Neue Haas Grotesk
Display font family and the real Firstsource logos.

---

## Architecture

```
Browser (static/)                  Server (app/)                    Anthropic API
─────────────────                  ─────────────                    ─────────────
index.html + app.js
   │
   │  POST /api/generate  ──────►  extract.py    read PDF/DOCX/PPTX/XLSX
   │  (multipart upload)           extract.py    merge into one corpus
   │                               knowledge.py  load knowledge/*.md  ┐
   │                               prompts.py    build system+user    ├──► messages.create()
   │                               llm.py        call the API, parse  ┘
   │  ◄──────────────────────────  schemas.py    validate the JSON
   │      validated document
   │
   │  POST /api/render/{fmt} ────► generators/   dashboard | qa | docx | pptx | xlsx
   │  ◄──────────────────────────  file download
```

Two deliberate design choices:

**Generate and render are separate calls.** The model runs once; the user can
then download two, three or all five formats, or re-download, without paying for
another model call. It also keeps the service stateless.

**The service is stateless.** Nothing is written to disk. No database, no session
store, no generated-file directory, no volumes to back up. Uploads live in memory
for the duration of the request only.

---

## File tree

```
firstsource-rfp-generator/
├── README.md                          this file
├── .env.example                       every environment variable, documented
├── .gitignore / .dockerignore
├── Dockerfile                         python:3.12-slim, non-root, healthcheck
├── docker-compose.yml                 single-service deployment
├── requirements.txt                   pinned dependencies
├── pytest.ini
├── run-dev.sh                         venv + install + reload server
│
├── app/                               ── BACKEND ──
│   ├── __init__.py
│   ├── main.py                        FastAPI app factory, middleware, mounts
│   ├── config.py                      all settings, read from the environment
│   ├── schemas.py                     Pydantic models for the model's output
│   ├── knowledge.py                   loads knowledge/*.md once at startup
│   ├── prompts.py                     terminology, mode and multi-document rules
│   ├── llm.py                         the ONLY module that calls Anthropic
│   ├── extract.py                     PDF/DOCX/PPTX/XLSX/text extraction
│   ├── brand.py                       colours, fonts, logos, footer
│   ├── auth.py                        auth modes and the session user
│   ├── routes/
│   │   ├── generate.py                POST /api/generate, /api/render/{fmt}
│   │   ├── health.py                  GET /healthz, /api/config
│   │   └── auth_routes.py             OIDC login/callback/logout/me
│   ├── generators/
│   │   ├── __init__.py                format registry
│   │   ├── _common.py                 filenames, media types, helpers
│   │   ├── dashboard.py               interactive HTML dashboard
│   │   ├── qa_doc.py                  Q&A response document
│   │   ├── word.py                    .docx  (python-docx)
│   │   ├── powerpoint.py              .pptx  (python-pptx)
│   │   └── excel.py                   .xlsx  (openpyxl)
│   └── templates/
│       ├── _brand.css.j2              shared brand stylesheet
│       ├── dashboard.html.j2          tabbed dashboard
│       └── qa.html.j2                 linear Q&A layout
│
├── knowledge/                         ── SERVER-SIDE ONLY, never sent to the browser ──
│   ├── analyst-knowledge-base.md      Firstsource RFI facts and figures
│   ├── kairos-glossary.md             Kairos / Intelligence that Operates glossary
│   └── brand-quick-reference.md       brand standards
│
├── static/                            ── FRONTEND ──
│   ├── index.html                     the whole UI
│   ├── css/app.css
│   ├── js/app.js                      calls our backend; no AI bridge
│   ├── logos/                         Firstsource dark + white, RPSG
│   └── fonts/                         Neue Haas Grotesk Roman/Medium/Bold/Black
│
└── tests/                             pytest suite; never calls the real API
    ├── conftest.py                    fixtures and sample payloads
    ├── test_api.py                    full request path, model stubbed
    ├── test_auth.py                   both auth modes, domain allow-list
    ├── test_extract.py                every file type + corpus rules
    ├── test_generators.py             all five formats, brand assertions
    ├── test_knowledge.py              loading + not-exposed-over-HTTP
    ├── test_llm.py                    JSON extraction from messy output
    └── test_prompts.py                terminology and multi-document rules
```

---

## Quick start (local)

Requires Python 3.11+ (3.12 recommended).

```bash
cd firstsource-rfp-generator

cp .env.example .env
#   edit .env and set ANTHROPIC_API_KEY=sk-ant-...

./run-dev.sh
```

Then open <http://localhost:8000>.

`run-dev.sh` creates `.venv`, installs `requirements.txt` and starts uvicorn with
auto-reload. To do it by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload --port 8000
```

Interactive API docs are at <http://localhost:8000/docs>.

---

## Running with Docker

```bash
cp .env.example .env      # set ANTHROPIC_API_KEY
docker compose up --build
```

The app listens on port 8000 inside the container, published to 8000 on the host.
Change the host port in `docker-compose.yml` if 8000 is taken:

```yaml
ports:
  - "8080:8000"           # host 8080 -> container 8000
```

Without compose:

```bash
docker build -t firstsource-rfp-generator:1.0.0 .
docker run -d --name rfp -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e PUBLIC_BASE_URL=https://rfp.yourcompany.com \
  -e ENVIRONMENT=production \
  firstsource-rfp-generator:1.0.0
```

The image runs as a non-root user and includes a `HEALTHCHECK` hitting
`/healthz`.

---

## Configuration

Every setting is an environment variable; `.env.example` is the full list. The
ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(none)* | **Required.** Server-side only. |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Any current model id. |
| `ANTHROPIC_MAX_TOKENS` | `16000` | Raise for very long questionnaires. |
| `ANTHROPIC_ENABLE_PROMPT_CACHE` | `true` | Caches the knowledge base between calls. |
| `ANTHROPIC_MAX_RETRIES` | `1` | SDK-level retries. Kept low so a timeout cannot compound. |
| `ENVIRONMENT` | `local` | `production` turns on extra config warnings. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Must match the real URL exactly; the SSO redirect URI is derived from it. |
| `PORT` | `8000` | |
| `AUTH_MODE` | `disabled` | `disabled` or `oidc`. |
| `SESSION_SECRET` | `change-me-in-production` | **Change it.** `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `SESSION_HTTPS_ONLY` | `false` | Set `true` once behind HTTPS. |
| `MAX_FILES` | `20` | Files per request. |
| `MAX_UPLOAD_MB` | `25` | Per file. |
| `MAX_PROMPT_CORPUS_CHARS` | `48000` | How much source text reaches the model. |
| `EMBED_FONTS_IN_HTML` | `true` | Base64-embeds the brand fonts in downloaded HTML (adds ~600 KB but the file stays on-brand anywhere). Set `false` for small files. |

On startup the app logs a warning for every risky setting (missing API key,
default session secret in production, unauthenticated production, non-HTTPS
cookies) and refuses to boot if the knowledge base is missing.

---

## Single sign-on

`AUTH_MODE=oidc` enables OpenID Connect. It is wired for Microsoft Entra ID
(Azure AD) and works with any compliant provider. No code changes are needed —
your engineering team supplies four values.

**1. Register the application** in Entra ID (App registrations → New
registration):

- Redirect URI (type **Web**): `https://rfp.yourcompany.com/auth/callback`
  — this must equal `PUBLIC_BASE_URL` + `/auth/callback`.
- Under *Certificates & secrets*, create a client secret.
- Under *Token configuration*, make sure the **email** claim is included;
  without it the app cannot identify users.

**2. Set the environment variables:**

```bash
AUTH_MODE=oidc
PUBLIC_BASE_URL=https://rfp.yourcompany.com
OIDC_DISCOVERY_URL=https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration
OIDC_CLIENT_ID=<application-client-id>
OIDC_CLIENT_SECRET=<client-secret>
OIDC_ALLOWED_EMAIL_DOMAINS=firstsource.com
SESSION_SECRET=<48-byte random string>
SESSION_HTTPS_ONLY=true
```

`OIDC_ALLOWED_EMAIL_DOMAINS` is an extra guard on top of whatever your IdP
enforces; leave it empty to accept any account the IdP authenticates.

The session is a signed, HTTP-only cookie holding only the user's email and
display name. There is no user database.

**If you use `AUTH_MODE=disabled`,** the application is completely open to anyone
who can reach the URL. That is only acceptable on a strictly intranet- or
VPN-only host.

---

## Moving to your own server

What actually changes between local testing and your deployment:

**1. Port.** Locally the app is on `8000`. On a server, keep the container on
8000 and let your reverse proxy own 80/443. Set `PORT` only if you need the app
itself on a different port.

**2. Domain and HTTPS.** The app speaks plain HTTP; terminate TLS in front of it
(nginx, Traefik, Caddy, or your corporate load balancer / Azure Application
Gateway). Then:

- Set `PUBLIC_BASE_URL=https://rfp.yourcompany.com` (exact, no trailing slash).
- Set `SESSION_HTTPS_ONLY=true` so the session cookie is HTTPS-only.
- Set `ENVIRONMENT=production`.
- Keep `--proxy-headers --forwarded-allow-ips *` in the uvicorn command (already
  in the Dockerfile) so the app sees the original scheme and client IP.

A minimal nginx server block:

```nginx
server {
    listen 443 ssl;
    server_name rfp.yourcompany.com;

    ssl_certificate     /etc/ssl/certs/rfp.crt;
    ssl_certificate_key /etc/ssl/private/rfp.key;

    # Uploads: must be at least MAX_FILES x MAX_UPLOAD_MB.
    client_max_body_size 550M;

    # Generation can take a couple of minutes on a long questionnaire.
    proxy_read_timeout 360s;
    proxy_send_timeout 360s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Two proxy settings people forget: `client_max_body_size` (default 1 MB will
reject uploads) and the read timeout (default 60s will cut off long generations).

**3. Where the API key comes from.** Locally it sits in `.env`. On a server, do
not ship `.env` — inject the key from your secret store:

- *Docker Compose*: `secrets:` or `environment:` populated by your deploy pipeline.
- *Kubernetes*: a `Secret` surfaced via `env.valueFrom.secretKeyRef`.
- *Azure*: App Service application settings, or Key Vault references.
- *systemd*: `EnvironmentFile=/etc/firstsource-rfp/env` with `chmod 600`.

The key is read once at startup from the process environment. It is never logged,
never returned by an endpoint and never sent to the browser.

**4. Outbound network access.** The server needs HTTPS egress to
`api.anthropic.com`. If you route through a proxy, set `HTTPS_PROXY` in the
container environment. Nothing else needs outbound access — fonts, logos and
styles are all served locally.

**5. Scaling.** Extraction and the model call both run in a worker thread
(`run_in_threadpool`), so one user's 90-second generation does not block anyone
else's request in the same process. `--workers 2` in the Dockerfile suits a sales team. Each worker
holds its own copy of the knowledge base (~50 KB), so raising it is cheap; 2–4
workers per CPU core is a reasonable rule. The app is stateless, so you can run
several replicas behind a load balancer with no session affinity required —
except that the SSO session cookie is signed with `SESSION_SECRET`, so **every
replica must share the same `SESSION_SECRET`**.

**6. Logs.** Everything goes to stdout/stderr for `docker logs` or your log
shipper. Each generation logs the title, mode, question count, source count and
corpus size against the signed-in user's email. Document *contents* are never
logged.

---

## Updating the knowledge base

The three files in `knowledge/` are the model's source of truth. To change them:

- **Rebuild the image** (they are copied in at build time), or
- **Mount them read-only** and restart, by uncommenting in `docker-compose.yml`:

  ```yaml
  volumes:
    - ./knowledge:/app/knowledge:ro
  ```

They are loaded once at startup and cached, so a restart is required either way.
`/healthz` reports `knowledge_base_chars` so you can confirm an update landed.

If a file grows beyond `MAX_KNOWLEDGE_CHARS_PER_FILE` (default 60,000) it is
truncated with a warning in the log — raise the limit rather than losing content.

---

## SharePoint knowledge base

The analyst knowledge base can be read directly from a SharePoint folder instead
of a local directory. Everything under the configured folder is indexed, at any
nesting depth, and the index keeps itself up to date.

### How it stays fresh

A single Microsoft Graph **delta query** does both jobs. The first call walks the
entire nested folder tree and returns a `deltaLink`; that link is stored in
Postgres. Every 15 minutes the app replays it and gets back only what changed —
files added, edited or deleted. When nothing has changed, that is one cheap HTTP
call and no further work.

Costs are avoided aggressively: a document is only re-processed when its content
actually changes (checked via SharePoint's `cTag`), so renaming a file or editing
a metadata column is free. Identical copies — SharePoint's `file (1).pdf` habit —
are indexed once.

Documents are identified by their SharePoint item ID, never by filename, so two
files with the same name in different subfolders do not collide, and renaming or
moving a file does not orphan its chunks.

### Configuration

```
RAG_SOURCE=sharepoint
RAG_SYNC_ENABLED=true
PG_ENABLED=true          # required: the delta link lives here
```

See `.env.example` for the full list and the SharePoint credentials.

### Commands

```bash
python test.py --delta            # read-only: show exactly what would be indexed
python -m app.rag.ingest          # incremental: only what changed
python -m app.rag.ingest --full   # re-enumerate everything
python -m app.rag.ingest --reset  # forget all state, then re-ingest everything
```

**Use `--reset` after wiping the Qdrant collection.** Without it, the stored
content tags make the next sync conclude "nothing changed" and the collection
stays empty.

### Limits

- Files are read wherever possible. Only audio, video, archives and executables
  are skipped (`.mp4`, `.zip`, …), plus `.doc`/`.ppt`, which have no reliable
  pure-Python reader — save those as `.docx`/`.pptx`.
- Change notifications (webhooks) are not used. They need a publicly reachable
  HTTPS endpoint; polling works from anywhere and is the safety net Microsoft
  recommends regardless.
- On Azure App Service, enable **Always On** or the app unloads when idle and
  stops polling.

## API reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The application UI |
| `GET` | `/healthz` | Liveness probe; reports knowledge-base status |
| `GET` | `/api/config` | Non-sensitive client config (formats, limits) |
| `POST` | `/api/generate` | multipart: `files[]`, `pasted`, `title`, `audience` → validated response document |
| `POST` | `/api/render/{fmt}` | JSON `{document, sources}` → file download. `fmt` ∈ `dashboard`, `qa`, `docx`, `pptx`, `xlsx` |
| `GET` | `/auth/login` · `/auth/callback` · `/auth/logout` · `/auth/me` | SSO |
| `GET` | `/docs` | OpenAPI UI |

Example:

```bash
# Generate
curl -s -X POST http://localhost:8000/api/generate \
  -F "files=@ClientRFI.docx" \
  -F "files=@Capabilities.pptx" \
  -F "title=Acme Bank RFI Response" \
  -F "audience=business" > result.json

# Render Word from that result
python3 - <<'PY' > response.docx
import json, urllib.request
r = json.load(open("result.json"))
body = json.dumps({"document": r["document"], "sources": r["sources"]}).encode()
req = urllib.request.Request("http://localhost:8000/api/render/docx", body,
                             {"Content-Type": "application/json"})
import sys; sys.stdout.buffer.write(urllib.request.urlopen(req).read())
PY
```

Error codes: `400` unreadable or empty input, `401` not signed in, `413` too many
or too-large files, `422` malformed request body, `502` the model call failed or
returned something unrenderable.

---

## Tests

```bash
source .venv/bin/activate
pytest                    # whole suite
pytest tests/test_generators.py -v
```

No test calls the real Anthropic API — `tests/test_api.py` swaps in a stub
client, so the suite is free to run and safe in CI. Coverage includes: every file
type round-tripped through extraction, multi-document corpus rules, terminology
rules present in the prompt, JSON recovery from fenced/prose-wrapped/trailing-comma
output, all five formats producing valid files with brand colours and the real
logo, HTML escaping of injected markup, Excel sheet-name limits, and both auth
modes.

---

### Before your first deploy

Two things to confirm in your own environment, because they depend on your
account and network rather than on this code:

1. **`ANTHROPIC_MODEL` resolves.** The default is `claude-sonnet-5`. Confirm the
   id against your account's available models and change the variable if needed —
   an unknown id surfaces only at generation time, as a `502`.
2. **Run the test suite once on the target host** (`pytest`). It needs no API key
   and no network, and it will catch a broken dependency resolution immediately.

## Operations and troubleshooting

| Symptom | Cause and fix |
|---|---|
| Startup error `Missing knowledge file(s)` | `knowledge/` was not copied or mounted. The app deliberately refuses to run without it. |
| `ANTHROPIC_API_KEY is not configured` on generate | The variable is missing from the process environment. Check with `docker exec <c> env \| grep ANTHROPIC`. |
| `502` with "Could not reach the Anthropic API" | No outbound HTTPS, or a proxy is required. Set `HTTPS_PROXY`. |
| `502` "did not return parseable JSON" | Usually an overly long or confusing input. The app already retries once; try splitting the source documents. |
| `413` on upload | Raise `MAX_FILES` / `MAX_UPLOAD_MB`, and `client_max_body_size` on the proxy. |
| Upload succeeds, "No readable text found" | Scanned or image-only PDF. Run OCR first, or paste the text. The UI names the offending file. |
| Long generation times out at ~60s | Reverse-proxy read timeout. Raise `proxy_read_timeout`. |
| SSO loops back to the login page | `PUBLIC_BASE_URL` does not exactly match the registered redirect URI, or the cookie is blocked because `SESSION_HTTPS_ONLY=true` on plain HTTP. |
| Logged in but immediately signed out | Replicas with different `SESSION_SECRET` values. Share one secret. |
| Downloaded HTML looks unstyled | `EMBED_FONTS_IN_HTML=false` and the viewer lacks the brand font. The fallback stack still applies; set it back to `true` for portability. |

---

## Cost and performance

Each generation is one Anthropic API call. The prompt carries the knowledge base
(~50 KB, roughly 13k tokens) plus up to `MAX_PROMPT_CORPUS_CHARS` of source text
(~12k tokens). With `ANTHROPIC_ENABLE_PROMPT_CACHE=true` the knowledge-base
portion is cached between requests, which is where most of the savings come from
for a team generating repeatedly.

Typical wall time is 30–90 seconds depending on how many questions there are.
Rendering a format afterwards is local work and takes well under a second, which
is why the UI can offer all five formats without extra model cost.

---

## What changed from the Cowork skill

| Cowork skill | This application |
|---|---|
| `window.cowork.askClaude()` in the browser | `POST /api/generate` → `app/llm.py` → Anthropic API |
| Knowledge base baked into the HTML, visible in page source | `knowledge/*.md` loaded server-side; never sent to the browser |
| `assets/lib/extractor.js` — hand-rolled ZIP/PDF parsing in the browser | `app/extract.py` — pypdf, python-docx, python-pptx, openpyxl |
| `assets/lib/officegen.js` — hand-built OOXML zips in the browser | `app/generators/` — python-docx, python-pptx, openpyxl |
| Needed Cowork to run | Any browser; nothing installed on the client |
| One user at a time | Whole team, behind SSO |
| No access control | OIDC with an email-domain allow-list |
| Model output parsed loosely in JS | Validated against Pydantic schemas before rendering |

Functionality is unchanged: multi-file upload, RFI vs. summary detection, Kairos
terminology enforcement, Firstsource brand, and all five output formats.

---

Copyright © 2026 Firstsource. All rights reserved.
