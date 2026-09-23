# Known Gaps & Improvement Backlog

Captured 2026-09-23, after a full read of the codebase against the two target
architecture infographics (`RFA_Architecture.png`, `Multimodal RAG Architecture
Infographic.png`).

**How to read this:** the infographics describe the TARGET architecture. Several
boxes in them are aspirational and have no code behind them yet. That is not a
defect — it is the roadmap. This file is the authoritative list of what is
diagram-only, what was deliberately substituted, and what is a genuine loose end,
so a future session can pick any item up without re-deriving it.

---

## 1. Diagram boxes with no implementation yet

### 1.1 Template-aware output generation (diagram box 10)
**Status:** not implemented.
**Diagram claims:** user uploads a reference template (Word/Excel/PPT); the system
maps generated fields into it, preserves formatting, applies client styles, fills
placeholders, validates mandatory fields, inserts charts.
**Reality:** `app/generators/` renders a fixed Firstsource-branded document from
`ResponseDocument`. There is no template ingest path, no placeholder mapping, no
field validation. The UI collects no reference template.
**Size:** large. Needs a template parser, a placeholder/field-mapping model, and a
formatting-preserving writer per format.

### 1.2 Citations and traceability (diagram box 12)
**Status:** not implemented as structured data.
**Diagram claims:** every answer carries source document name, page/slide/sheet
number, exact text snippet, confidence score, and a hyperlink back to the source.
**Reality:** `ResponseDocument` / `QAItem` in `app/schemas.py` have no citation,
page, confidence or URL fields. `app/prompts.py` asks the model in prose to "name
the source document"; nothing is structured, verified or linkable. The retrieval
layer DOES carry the needed data (`RetrievedChunk.source`, `.location`, `.web_url`)
— it is discarded at generation time.
**Size:** medium. Mostly a schema + prompt + renderer change; the data already exists.

### 1.3 Query understanding (diagram box 6)
**Status:** not implemented.
**Diagram claims:** intent detection, entity extraction, query-type classification,
multi-turn detection.
**Reality:** the retrieval query is literally `prompt_corpus[:8000]` — the first
8000 characters of the uploaded corpus (`app/routes/generate.py`). There is no
query construction step at all. For an RFI with 30 questions, one blunt 8000-char
query is issued, not one query per question.
**Size:** medium-large, and probably the highest-value retrieval improvement
available. Per-question retrieval would likely move the eval scorecard more than
any further vector work.

---

## 2. Deliberate substitutions (documented decisions, not defects)

| Diagram says | We run | Why |
|---|---|---|
| `bge-reranker-v2-m3` | `BAAI/bge-reranker-base` via fastembed | v2-m3 needs sentence-transformers + torch; too heavy for the App Service plan. Same family, ONNX, no GPU. Revisit by running the larger model as a service on the Azure VM if eval shows the reranker is the bottleneck. |
| ColQwen2.5 visual embeddings | none | Explicitly deferred by the user. Images already flow in as GPT-5.4-vision-generated text through the normal text path. Revisit only if evaluation shows a concrete gap. |
| Semantic cache in PostgreSQL | cache vectors + payload in a separate Qdrant collection; Postgres stores hit/write audit rows only | Avoids requiring pgvector on an RDS instance shared with another project. See `app/rag/cache.py` module docstring. |
| Cached *answers* | cached *retrieval results* | The cache short-circuits embedding + hybrid search + rerank. The LLM generation call still runs every time. Caching final answers is a separate, larger decision (correctness/staleness of generated prose). |

---

## 3. Genuine loose ends

### 3.1 `_match_known_source` almost never fires
`app/rag/retrieve.py` hard-filters retrieval to one document when the query
contains a known filename stem as a literal substring (min 6 chars). Real filenames
(`Saas_Award_2026_HFS`) do not appear verbatim in natural questions, so the
document-routing feature — the user's explicit Phase 4 worked example — is
effectively dead in practice. Needs fuzzy/token-overlap matching, or an LLM-extracted
document-name entity from the query, to actually work.

### 3.2 `azure_openai_max_tokens` is a dead setting
Configured in `app/config.py` and `.env` (16000), never passed to
`chat.completions.create` in `app/llm.py`. Either wire it up or delete it.

### 3.3 `qdrant_collection` is commented out in config
`app/config.py` still carries a commented-out `qdrant_collection` line from before
the v2 multi-vector migration. The old collection `firstsource_analyst_kb_test` may
still exist on the Qdrant VM. Decide: keep for rollback, or delete and remove the
dead config line.

### 3.4 Per-chunk ingestion cost is unbounded
`index_document` makes one LLM call per chunk for `dense_summary`, plus one more
per table-heavy chunk. Accepted deliberately (no meaningful Azure quota constraint
at current corpus size), but it scales linearly with corpus size and will become the
dominant cost/time factor on a large SharePoint library. Batching, or skipping the
summary vector for very short chunks, is the obvious lever if it starts to hurt.

### 3.5 No chunk deletion path
Re-ingesting a document upserts by deterministic UUID5 (`source::chunk_index`). If a
document shrinks from 40 chunks to 20, chunks 20-39 from the previous version stay in
Qdrant forever as orphans. There is no delete-by-source, and no handling for a
document removed from the source folder entirely.
*(Being addressed as part of the SharePoint ingestion work — remove this entry once
that lands.)*

### 3.6 Everything is uncommitted
As of this writing, all of Phases 1-4 sit uncommitted on
`feature/rag-sharepoint-grounding` (13 modified files, ~10 new, including the whole
of `app/rag/`). Only `700c459 Initial commit` exists in history.

---

## 4. Suggested priority if picking this up cold

1. **3.5 / chunk deletion** — correctness bug, cheap to fix.
2. **1.3 / per-question retrieval** — largest expected quality gain per unit of work.
3. **1.2 / structured citations** — high user-visible value, data already retrieved.
4. **3.1 / document routing** — makes an already-built Phase 4 feature actually work.
5. **1.1 / template-aware output** — largest scope; its own spec and phase.
