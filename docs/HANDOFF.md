# Handoff — Firstsource RFP Response Generator

**Updated 2026-09-28.** Read this first, then `docs/known-gaps-and-backlog.md`.
Everything below is verified state, not plans.

---

## 1. What this project is

A FastAPI app that turns analyst RFIs and questionnaires into on-brand Firstsource
deliverables (dashboard, Q&A doc, Word, PowerPoint, Excel). Answers are grounded in
a RAG pipeline over a SharePoint analyst document library.

- **Branch:** `feature/rag-sharepoint-grounding`
- **Stack:** Azure OpenAI (`gpt-5.4` chat+vision, `text-embedding-3-large`, on two
  SEPARATE resources with different keys), Qdrant on an Azure VM, PostgreSQL on AWS RDS.
- **Tests:** 231 passing. `./.venv/Scripts/python.exe -m pytest tests/` (~30s).
  The suite is hermetic — it never touches live Qdrant/Postgres/Azure.
- **Dependencies:** `requirements.txt` is what the server installs.
  `requirements-dev.txt` adds pytest and the DeepEval harness — install that locally.

---

## 2. Current state

| Thing | State |
|---|---|
| Deployment | **Live** on Azure App Service `RFA-generator` (see section 3) |
| SharePoint ingestion | **Complete.** Latest sync: 109 unchanged, 3 duplicates, 0 failed |
| Qdrant collection | `firstsource_analyst_kb_v2` (old `firstsource_analyst_kb_test` still exists, unused) |
| `RAG_SOURCE` | `sharepoint` |
| `RAG_SYNC_ENABLED` | `true` — 15-min delta sync + 24h full reconcile, running on Azure |
| `AUTH_MODE` | `password` — users `admin@firstsource.com`, `niranjan@firstsource.com` |
| Retrieval tuning | `rag_rerank_top_k=8`, `rag_rerank_score_margin=6.0`, `rag_per_question_top_k=8` |
| Latest eval | **Run 8** (2026-09-25/26, 60 questions) — see section 4 |

**If a sync is killed**, its Postgres advisory lock can linger and the next run
logs "Another instance is already syncing". Clear it:

```sql
SELECT pg_terminate_backend(pid) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype='advisory' AND l.objid=728411 AND a.state='idle';
```

Running the app locally with `RAG_SYNC_ENABLED=true` at the same time as Azure is
safe — the advisory lock lets only one of them sync at a time.

---

## 3. Deployment (Azure App Service)

- **URL:** https://rfa-generator-fkajaqh8crhqeyc3.eastus-01.azurewebsites.net
- **Resource:** `RFA-generator`, resource group `DataScienceTeam-Internal`,
  plan `agenticaidemos` (B3, Linux, Python 3.11). Always On is enabled.
- **Startup command** (set in Azure, mirrors `startup.sh`):
  `gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --workers 1 --bind 0.0.0.0:$PORT --timeout 600`
- **Memory:** the plan is a shared 7 GB B3 hosting ~160 apps. The reranker needs
  ~2.4 GB per worker, hence 1 worker. Reranks run one at a time in batches of 16
  (`rag_rerank_batch_size`): 4 parallel reranks at batch 64 reached 7.4 GB and got
  the worker OOM-killed mid-generation (2026-09-28). Scores are identical at any
  batch size, and 16 was also the fastest on CPU (21 s vs 28 s at 64, per question).
- **App settings** mirror the local `.env`, except Azure keeps its own
  `ENVIRONMENT`, `SESSION_SECRET` and `SESSION_HTTPS_ONLY`, and adds:
  - `PUBLIC_BASE_URL` = the Azure URL
  - `AUTH_USERS_FILE=/home/data/users.json` — `/home` survives restarts and redeploys;
    the code folder does not. Upload a changed `users.json` there via Kudu
    (`https://rfa-generator-fkajaqh8crhqeyc3.scm.eastus-01.azurewebsites.net/api/vfs/data/users.json`).
  - `FASTEMBED_CACHE_PATH=/home/data/fastembed_cache` — the ~1 GB reranker model is
    downloaded once, not on every restart.
- **Deploying:** zip `app/`, `knowledge/`, `static/` and `requirements.txt`, then
  `az webapp deploy -g DataScienceTeam-Internal -n RFA-generator --src-path deploy.zip --type zip`.
  Azure builds it (Oryx). The CLI often reports "site failed to start" even when it
  started — check `/healthz` and `.../api/deployments/latest` on the Kudu site instead.
- **Logs:** container logging to the filesystem is on. App output is in
  `LogFiles/<date>_<machine>_default_docker.log` on the Kudu site.
- **Long generations:** Azure's front end drops any request open longer than ~230 s,
  and a full RFI takes 10+ minutes. So the browser calls `POST /api/jobs`, gets a
  job id, and polls `GET /api/jobs/{id}` (`app/jobs.py`). `POST /api/generate`
  still works in one request, for local and scripted use. A running job heartbeats
  its file every 20 s; if the worker dies, the browser is told within ~2 minutes.
  Reading the uploaded files also happens inside the job: every embedded image
  goes to vision, and a form-style Word file (200 copies of two checkbox icons)
  took 5+ minutes and produced a 504 when reading was done in the request.
  Identical pictures inside one Word file are now read once.
- **Speed lever not yet tested:** ranking fewer candidates (`rag_retrieve_top_k`
  40 instead of 100) would be ~2.5x faster but may change which evidence is picked.
  A reranker on a GPU VM (optionally bge-reranker-v2-m3) is the larger option.
- Postgres and Qdrant are publicly reachable, so no firewall rules were needed.

---

## 4. Evaluation

| Metric | Run 6 | Run 7 | **Run 8 (latest)** |
|---|---|---|---|
| Faithfulness | 0.79 / 87% | 0.88 / 94% | **0.88 / 95%** |
| AnswerRelevancy | 0.85 / 100% | 0.96 / 100% | **0.90 / 97%** |
| ContextualPrecision | 0.96 / 100% | 0.78 / 86% | **0.82 / 90%** |
| ContextualRecall | 0.94 / 96% | 0.69 / 74% | **0.74 / 80%** |
| ContextualRelevancy | 0.44 / 37% | 0.54 / 53% | **0.50 / 48%** |

(average score / pass rate; results in the `eval_runs` / `eval_results` tables)

Run 8 predates the final retrieval tuning (rerank 8 / margin 6.0, see
`tests/eval/tuning_results.json`) and per-question retrieval, so the live
configuration has not had a full scored run. ContextualRelevancy (how much
retrieved text is on-topic) remains the weakest metric.

The eval is slow and costs tokens — only run it when asked:

```bash
./.venv/Scripts/python.exe -m tests.eval.generate_dataset --per-document 3 --max-documents 20
./.venv/Scripts/python.exe -m tests.eval.run_eval
./.venv/Scripts/python.exe -m tests.eval.run_eval --resume-run <id>   # after a network drop
```

---

## 5. Done since the previous handoff (2026-09-24)

- Ingestion finished; the latest sync reports 0 failures.
- Near-duplicate chunk suppression at retrieval time (sibling drafts of one survey).
- Per-question retrieval: RFI questions are detected (keeping their numbering and
  where they came from), rewritten when vague, and searched one by one.
- Orphan sweep for files under a deleted SharePoint folder.
- Retrieval tuning (above). Background sync switched on.
- Live quality badge (`app/quality.py`) now sees the full reference material; it
  was cut at 20k characters and gave false low scores.
- Deployed to Azure, with generation as a background job (section 3).

---

## 6. Deliberately left as-is (decided — do not re-open)

Template-aware output; structured citation/confidence fields; extra Postgres
tables (templates, audit, roles); RBAC/MFA; document-name routing and re-ranking
diversity beyond near-duplicate removal; dead `azure_openai_max_tokens`; the old
Qdrant collection; the deferred review findings; the demo password and
`SESSION_HTTPS_ONLY`; a GPT retry for missing image descriptions/summaries.

**Open, for the user to decide:** answers name other clients (Humana, LBG, …)
through source file names — fine internally, but likely unwanted in a response
sent to a different client or analyst.

---

## 7. Constraints — established, do not re-litigate

- **No new Microsoft/Azure resources** can be provisioned by the user. Work with
  the existing app registration and its read-only access to one SharePoint folder.
- **Azure OpenAI has no meaningful quota limit** for this corpus. Extra LLM calls
  during ingestion are acceptable — the user chose full quality over cost.
- **Avoid heavy automated test runs.** The pytest suite is cheap and fine; the
  *eval* is expensive and slow.
- **The user's network has dropped during long jobs.** Any multi-hour local run
  needs the laptop set to never sleep, and should be expected to need a resume.
- File-type policy: read everything reasonably readable; reject only `.mp4`/`.zip`
  and similar binaries. `.html` and `.xls` are in scope.
- Communication: lead with plain language, detail after.

---

## 8. Useful commands

```bash
# tests (hermetic)
./.venv/Scripts/python.exe -m pytest tests/

# see exactly what SharePoint would ingest - read-only, no downloads, no tokens
RAG_SOURCE=sharepoint ./.venv/Scripts/python.exe test.py --delta

# manual full sync (normally the scheduler does this)
./.venv/Scripts/python.exe -u -m app.rag.ingest --full > ingest.log 2>&1

# users (then upload users.json to /home/data/ on Azure)
./.venv/Scripts/python.exe -m app.users add you@firstsource.com
```
