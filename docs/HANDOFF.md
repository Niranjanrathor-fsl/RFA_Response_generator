# Handoff — Firstsource RFP Response Generator

**Written 2026-09-24.** Read this first, then `docs/known-gaps-and-backlog.md`.
Everything below is verified state, not plans.

---

## 1. What this project is

A FastAPI app that turns analyst RFIs and questionnaires into on-brand Firstsource
deliverables (dashboard, Q&A doc, Word, PowerPoint, Excel). Answers are grounded in
a RAG pipeline over a SharePoint analyst document library.

- **Branch:** `feature/rag-sharepoint-grounding`
- **Stack:** Azure OpenAI (`gpt-5.4` chat+vision, `text-embedding-3-large`, on two
  SEPARATE resources with different keys), Qdrant on an Azure VM, PostgreSQL on AWS RDS.
- **Tests:** 183 passing. `./.venv/Scripts/python.exe -m pytest tests/` (~27s).
  The suite is hermetic — it never touches live Qdrant/Postgres/Azure.

---

## 2. Current state — READ BEFORE CHANGING ANYTHING

| Thing | State |
|---|---|
| SharePoint ingestion | **52 of 113 documents indexed, 6,425 vectors.** Incomplete. |
| Qdrant collection | `firstsource_analyst_kb_v2` (old `firstsource_analyst_kb_test` still exists, unused) |
| Delta link | **Not saved** — correct, because documents still fail. Next run re-enumerates. |
| `RAG_SOURCE` | `sharepoint` |
| `RAG_SYNC_ENABLED` | `false` — the 15-min background sync is NOT on yet |
| `AUTH_MODE` | `password` (was `disabled`; original `.env` saved at `.env.before-auth-demo`) |
| Postgres backup | `documents_backup_20260923` table, taken before the schema migration |
| Eval run id | **7** in the `eval_runs` / `eval_results` tables — 47 questions, complete |

**To resume ingestion** (safe, resumable, skips completed documents):

```bash
python -m app.rag.ingest --full
```

Do NOT use `--reset` unless the Qdrant collection has just been wiped — it clears
every content tag and re-pays for all 52 finished documents.

**If a run is killed**, its Postgres advisory lock can linger and the next run
logs "Another instance is already syncing". Clear it:

```sql
SELECT pg_terminate_backend(pid) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype='advisory' AND l.objid=728411 AND a.state='idle';
```

---

## 3. THE MAIN TASK: improve the evaluation scores

This is what the next session should work on.

### Where we are (eval run 7, 47 questions, current config)

| Metric | Old baseline | Now | Verdict |
|---|---|---|---|
| AnswerRelevancy | 0.85 / 100% | **0.96 / 100%** | good |
| Faithfulness | 0.79 / 87% | **0.88 / 94%** | good |
| **ContextualRelevancy** | 0.44 / 37% | **0.54 / 53%** | improved, still the worst |
| ContextualPrecision | 0.96 / 100% | **0.78 / 86%** | dropped |
| ContextualRecall | 0.94 / 96% | **0.69 / 74%** | dropped most |

Failures: ContextualRelevancy 22, ContextualRecall 11, ContextualPrecision 6,
Faithfulness 3.

### What the metrics mean here

- **ContextualRelevancy** (worst): of the chunks retrieved, how much is actually
  on-topic. Low = we retrieve a lot of noise alongside the answer.
- **ContextualRecall**: was the information needed to answer actually retrieved at
  all. Low = the right chunk never came back.
- **ContextualPrecision**: were the most relevant chunks ranked highest.

Generation is healthy. **Retrieval is the problem.**

### The critical caveat — do not skip this

**Only 46% of the corpus is indexed.** A meaningful share of ContextualRecall
failures are probably questions whose source document simply is not in the index
yet. Tuning retrieval against a half-built index means tuning against noise.

**Recommended order:**

1. **Finish the ingestion first** (`python -m app.rag.ingest --full`, several hours).
2. **Regenerate the eval set** so it covers the whole corpus, not just Avasant:
   `python -m tests.eval.generate_dataset --per-document 3 --max-documents 20`
   (it already restricts itself to indexed documents).
3. **Re-run the eval** to get a trustworthy baseline: `python -m tests.eval.run_eval`
4. **Only then tune.**

### Levers, cheapest first

| Lever | Cost to change | Notes |
|---|---|---|
| `rag_rerank_top_k` (8) | **free, instant** | Query-time. Lower = less noise, risks recall. |
| `rag_rerank_score_margin` (6.0) | **free, instant** | Query-time. Lower = tighter cut. |
| `rag_retrieve_top_k` (100) | **free, instant** | Candidates before rerank. |
| Near-duplicate suppression | code change | See below — likely the real win. |
| `rag_chunk_tokens` (200) | **~13h re-ingest** | Ingestion-time. Last resort. |

**Never change chunk size before exhausting the query-time levers** — it invalidates
every vector and requires a full re-index.

### A concrete hypothesis worth testing first

The corpus contains genuine **near-duplicate documents** — e.g.
`Avasant_Applied AI Services_2026_RadarView_Survey_1.docx`,
`..._Survey_v1.docx`, `..._RFI_DraftResponses_v2.docx`. These are different drafts of
the same survey, so they are NOT byte-identical and the existing content-hash dedup
(`rag_skip_duplicate_content`) correctly does not catch them.

Effect: one question retrieves the same passage from four drafts. That is exactly
what ContextualRelevancy and ContextualPrecision penalise, and it also crowds out
genuinely different chunks, hurting recall.

**Test it before building anything:** take a failing question from `eval_results`
(run 7), call `app.rag.retrieve.search()` on it, and print the retrieved chunks with
their `source`. If several are near-identical text from sibling documents, the fix is
near-duplicate suppression at retrieval time (drop a chunk whose text closely matches
one already selected), not a rerank tweak.

---

## 4. What was completed this session

All committed on `feature/rag-sharepoint-grounding`.

**SharePoint delta sync** (spec: `docs/superpowers/specs/2026-09-23-sharepoint-delta-sync-design.md`,
plan: `docs/superpowers/plans/2026-09-23-sharepoint-delta-sync.md`)
- Recursive ingestion of the whole nested folder tree via the Graph delta query
  (the old code explicitly skipped subfolders). 113 ingestable files across 23 folders.
- Incremental sync keyed on SharePoint's `cTag`, so unchanged documents cost nothing.
- Deletions handled; chunks removed before re-index, closing the orphan-chunk gap.
- Identity moved from filename to SharePoint item id — two same-named files in
  different folders no longer overwrite each other.
- Background scheduler (15 min + 24h reconcile), currently disabled.

**Bugs found by running it, each with a test**
- Delta responses do **not** include `@microsoft.graph.downloadUrl` (only `/children`
  does) — every file was silently skipped. Now uses the authenticated `/content` endpoint.
- A 132-chunk document is ~10.8 MB in one upsert; the Qdrant VM timed out on it every
  time. Now batched at 32 points per request.
- `410 Gone` delta-token expiry was specified but unimplemented — would have frozen
  the index permanently.
- A failed reconcile recorded itself as successful, deferring the next one 24h.
- The advisory lock was held by dead sessions for 1h35m; then the keepalive "fix"
  made a *live* session lose it. Now heartbeated.
- A network outage failed all 99 remaining documents in one second, nearly burning
  every document's retry budget. Now aborts after 5 consecutive failures.
- The test suite was **not hermetic** — DeepEval's pytest plugin calls `load_dotenv()`
  before conftest, so `os.environ.setdefault` was a no-op and tests ran against live
  Qdrant/Postgres/Azure. Fixed; suite went 458s → 20s.

**Other**
- `.html` and legacy `.xls` are now readable; only true binaries (`.mp4`, `.zip`, …)
  are rejected.
- **Image uploads** (PNG/JPG/GIF/WebP/BMP) work in the UI, read by GPT-5.4 vision.
- Dropzone wording fixed — it showed the alphabetically-first 8 extensions,
  omitting PDF/Word/PowerPoint entirely.
- **`AUTH_MODE=password`** — username/password sign-in with salted scrypt hashes,
  no new dependency. `python -m app.users add|list|remove`.
- The sign-in gate never actually hid the app: `.app{display:grid}` outranked
  `[hidden]`. Latent since the gate was written.

---

## 5. Everything still outstanding

**Rollout**
1. Finish ingestion (61 documents remain)
2. Fix 2 known ingestion failures:
   - `Forrester responses.XLS` — named `.XLS` but is really `.xlsx`; needs a
     fallback to openpyxl when xlrd reports "Excel xlsx file; not supported"
   - `ISG Generative AI Services 2025.xlsx` — a chunk exceeds the embedding
     endpoint's max input size; needs a length guard before embedding
3. Spot-check retrieval on a nested-subfolder question
4. Improve eval scores (section 3)
5. `RAG_SYNC_ENABLED=true`, restart, verify a new upload appears within 15 min
6. Verify deletion removes it
7. **Unverified:** does Graph delta report files under a DELETED FOLDER? If not, an
   orphan sweep is needed. Requires deleting a test folder in SharePoint.

**Deferred review findings**
8. Deleting one of two identical files leaves the survivor unindexed until reconcile
9. Migration SQL has no executed test
10. `mark_document_deleted` filters on `item_id` without `source_type`
11. Semantic cache silently degrades to a plain TTL cache when Postgres is off
12. `documents_deleted` overcounts (counts folders)

**Backlog** (`docs/known-gaps-and-backlog.md`)
13. Per-question retrieval — the retrieval query is currently `prompt_corpus[:8000]`,
    one blunt query for a whole RFI. Likely the single biggest quality win.
14. Structured citations — retrieval already carries source/page; generation discards it
15. `_match_known_source` matches filenames literally, so it almost never fires
16. Ingestion cost controls (one LLM call per chunk; this is why a full run takes ~13h)
17. Template-aware output generation
18. `azure_openai_max_tokens` is configured but never passed to the API
19. Decide the fate of the old `firstsource_analyst_kb_test` collection

**Housekeeping**
20. Phase 1–4 work still uncommitted (UI, prompts, schemas, `quality.py`, `tests/eval`)
21. Branch never merged or pushed
22. Graph webhooks — possible once there is a public HTTPS URL
23. **Change the demo password** (`niranjan@firstsource.com` / `firstsource-demo-2026`)
    and set `SESSION_HTTPS_ONLY=true` before any public deployment

---

## 6. Constraints — established, do not re-litigate

- **Local only.** No public HTTPS URL, so Graph webhooks are impossible; polling is
  the trigger. Azure deployment is planned but not done.
- **No new Microsoft/Azure resources** can be provisioned. Work with the existing app
  registration and its read-only access to one SharePoint folder.
- **Azure OpenAI has no meaningful quota limit** for this corpus. Extra LLM calls
  during ingestion are acceptable — the user chose full quality over cost.
- **Avoid heavy automated test runs** to conserve subscription budget. The pytest
  suite is cheap (20s, hermetic) and fine; the *eval* is expensive and slow.
- **The user's network has dropped twice** during long jobs. Any multi-hour run needs
  the laptop set to never sleep, and should be expected to need a resume.
- File-type policy: read everything reasonably readable; reject only `.mp4`/`.zip`
  and similar binaries. `.html` and `.xls` are explicitly in scope.
- Communication: lead with plain language, detail after.

---

## 7. Useful commands

```bash
# tests (hermetic, ~27s)
./.venv/Scripts/python.exe -m pytest tests/

# see exactly what SharePoint would ingest - read-only, no downloads, no tokens
RAG_SOURCE=sharepoint ./.venv/Scripts/python.exe test.py --delta

# resume ingestion
./.venv/Scripts/python.exe -u -m app.rag.ingest --full > ingest.log 2>&1

# eval
./.venv/Scripts/python.exe -m tests.eval.generate_dataset --per-document 3 --max-documents 20
./.venv/Scripts/python.exe -m tests.eval.run_eval
./.venv/Scripts/python.exe -m tests.eval.run_eval --resume-run <id>   # after a network drop

# users
./.venv/Scripts/python.exe -m app.users add you@firstsource.com
```
