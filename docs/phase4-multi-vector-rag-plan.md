# Phase 4 — Advanced Multi-Vector RAG: Complete Implementation Plan

> **Purpose of this document:** This is a fully self-contained plan, written so that
> a new chat session (in this or any other Copilot/AI subscription) can read it and
> implement Phase 4 correctly without needing prior conversation history. It includes
> the full context of the project, what's already built, what's being added, and the
> exact files/functions to change.

---

## 1. Project context (read this first)

This is the **Firstsource RFP Response Generator** — a FastAPI app that turns RFIs/
questionnaires/source documents into on-brand Firstsource deliverables (dashboard,
Q&A doc, Word, PowerPoint, Excel). It uses **Azure OpenAI** (`gpt-5.4` for chat+vision,
`text-embedding-3-large` for embeddings, on two SEPARATE Azure OpenAI resources with
different keys/endpoints — see `app/config.py` `azure_openai_*` vs
`azure_openai_embedding_*` settings).

### What's already built (Phases 1–3, all live-validated and working)

| Phase | What it added | Key files |
|---|---|---|
| **1 — MVP retrieval** | SharePoint/local doc ingestion → structure-aware chunking → hybrid (dense+BM25) retrieval → cross-encoder rerank → grounded generation | `app/rag/sources.py`, `app/rag/chunking.py`, `app/rag/embeddings.py`, `app/rag/index.py`, `app/rag/retrieve.py`, `app/rag/ingest.py` |
| **2 — Metadata + Evaluation** | Postgres tracking (documents, ingestion runs, eval results) + DeepEval scorecard (Faithfulness, Answer Relevancy, Contextual Precision/Recall/Relevancy), auto-generating eval dataset via DeepEval Synthesizer | `app/rag/store.py`, `tests/eval/*.py` |
| **3 — Vision/multimodal** | Images/charts/scanned pages described via GPT-5.4 vision (single call does BOTH description AND decorative-vs-meaningful classification — no pixel-size content filtering); native chart data extraction for PPTX/XLSX (exact numbers, no vision guessing); comprehensive per-format extraction (grouped shapes, headers/footers, per-page PDF images) shared between the interactive upload path and the RAG ingestion path | `app/document_sections.py` (shared extraction module), `app/rag/vision.py` |

### Current infrastructure (all real, live, already working — reuse, don't recreate)
- **Qdrant** (self-hosted on user's Azure VM): `http://172.203.216.197:8010`, API key set.
  Current collection: `firstsource_analyst_kb_test`. Currently has **2 named vectors**
  per point: `dense` (from `text-embedding-3-large`, 3072-dim) and `sparse` (BM25 via
  fastembed `Qdrant/bm25`).
- **PostgreSQL** (AWS RDS): tables `documents`, `document_versions`, `ingestion_runs`,
  `eval_runs`, `eval_results` in the `public` schema. See `app/rag/store.py`.
- **Reranker**: `BAAI/bge-reranker-base` via `fastembed` (a lighter substitute for
  `bge-reranker-v2-m3`, which needs torch — deliberately avoided for the App Service
  plan). See `app/rag/retrieve.py`.
- **Sample corpus**: `knowledge/rag_sample_docs/` — currently ~16 files spanning
  `.md .txt .csv .json .docx .pptx .xlsx .pdf .png`, including a real analyst award
  submission (`Saas_Award_2026_HFS.docx`) with mixed text+table+chart+image content.

### Confirmed via live testing (do not re-verify, these are established facts)
- Qdrant supports **per-point optional/partial named vectors** — a point does not
  need to provide every named vector declared in the collection schema. A query
  against a vector name a point lacks simply never matches that point. **Verified
  live** with an in-memory Qdrant instance (see test in the chat history that produced
  this doc) — this is the technical foundation the table-vector design below depends on.
- Qdrant collections have a **fixed vector schema at creation time** — you cannot add
  a new named vector to an existing collection. Any schema change requires creating a
  **new collection** and re-ingesting (see Migration section below).
- Azure OpenAI usage has **no meaningful quota/cost constraint** for this corpus size
  (user-confirmed) — extra LLM calls during ingestion are acceptable and expected.
- The user does **not** want a separate visual embedding model (ColQwen2.5) — deferred
  indefinitely unless evaluation shows a concrete gap. Do not build it as part of Phase 4.

---

## 2. What Phase 4 is: the actual ask

The user wants **advanced multi-vector RAG** in this specific sense (their own quoted
definition, and this document takes it as the authoritative spec):

> "Multi-Vector RAG decouples the data used for finding matches from the actual data
> passed to the LLM. Instead of relying on a single, flattened embedding vector for
> every chunk of text, it creates and stores multiple representations — separate
> summary vectors — linked back to a larger or raw source document."

And a concrete worked example the user gave (**treat this as a hard requirement**):

> "For example, a metadata vector should also store document details and other
> information so that if a client-specific or other point-specific detail is asked
> from a specific document as a query, it can help retrieve that and improve accuracy
> and relevance."

This means: **multiple named vectors per chunk**, each capturing a different retrieval
signal, all linked to the same underlying chunk, fused at query time — with the FULL
original chunk (not a lossy summary) always being what's sent to the LLM.

---

## 3. The vector schema to implement

Each chunk becomes ONE Qdrant point with the following **named vectors**:

| Vector name | What it embeds | Always present? | Purpose |
|---|---|---|---|
| `dense_content` (rename of today's `dense`) | The full chunk text (current behavior, unchanged) | Yes | General semantic match on the complete content |
| `sparse_bm25` (rename of today's `sparse`) | Same chunk text, BM25 | Yes | Exact keyword/number/acronym match |
| `dense_summary` **(NEW)** | A short LLM-generated retrieval proxy: 1-2 sentence summary OR 3-5 "hypothetical questions this chunk answers" | Yes | **This is the core "decouple matching from LLM context" mechanism.** Short, focused text embeds and matches natural-language queries far better than a long chunk diluted by tables/numbers. The full original chunk is still what gets sent to the LLM — only the *matching* vector is different. |
| `dense_table` **(NEW, conditional)** | A natural-language paraphrase of tabular content (e.g. "This table compares pricing tiers: Starter at $15K/month...") — only generated for chunks classified as table-heavy | No — only present on table-heavy chunks | Raw `"col | col | col"` pipe-delimited text embeds poorly (embedding models are trained mostly on prose); a paraphrase embeds much better while the real table stays intact in the payload/LLM context |
| `dense_metadata` **(NEW)** | An LLM-generated **document-identity summary**, computed ONCE per source document and copied onto every chunk from that document: source filename, document type, topic/client/award-category, and key named entities/products mentioned (e.g. "Kairos", "UnBPO", "Mortgage Language Model") | Yes | **This is the user's explicit worked example.** Lets a query that names a document, client, product, or topic match strongly even if that exact chunk's own text doesn't repeat those words densely. |

Plus **richer payload** (metadata, not vectors — used for filtering, not just display):
```json
{
  "source": "Saas_Award_2026_HFS.docx",
  "location": "Image 1",
  "content_type": "text | table | image | mixed",
  "document_type": "docx | pptx | xlsx | pdf | image | csv | json | text",
  "chunk_index": 4,
  "web_url": "...",
  "modified_at": "...",
  "text": "<the full original chunk — unchanged from today>"
}
```

### Why this design and not something else (the reasoning, for whoever implements this)
- **No new model, no new infrastructure.** Everything above uses Qdrant (already
  running) + `gpt-5.4` (already the chat deployment) + `text-embedding-3-large`
  (already the embedding deployment). This directly satisfies "multi-vector" without
  ColQwen2.5, which the user explicitly does not want built right now.
- **`dense_summary` is the headline fix** for a real, observed weakness: in the last
  eval run, `ContextualPrecisionMetric` had two borderline failures (0.43, 0.49,
  just under the 0.5 threshold) specifically on comparison-style questions
  ("domain LLM vs foundational model"). A focused summary/hypothetical-question
  vector should directly improve ranking precision for exactly this failure mode.
- **`dense_metadata` solves the user's literal ask**: document-specific/client-specific
  queries. Example from the real corpus: a query like *"What does the SaaS Award
  submission say about UnBPO?"* should strongly match chunks from
  `Saas_Award_2026_HFS.docx` even if the word "UnBPO" only appears once in a specific
  chunk — because the document-level identity vector carries that association for
  every chunk from that file.
- **`dense_table` addresses a documented, known weakness**: raw pipe-delimited rows
  (e.g. `"Tier | Monthly Fee | Included Volume\nStarter | 15000 | 5,000 transactions"`)
  are out-of-distribution for embedding models trained mostly on natural language.
  A paraphrase (e.g. *"This pricing table shows three tiers: Starter at $15,000/month
  including 5,000 transactions..."*) embeds far more meaningfully.

---

## 4. Query-time retrieval logic

### 4a. Document-name/entity routing (hard filter, cheap, no LLM call)
Before running vector search, check whether the query text contains a recognizable
document filename (case-insensitive, ignoring extension, fuzzy-tolerant) from the set
of already-ingested documents. **Reuse the existing Postgres `documents` table**
(`app/rag/store.py`) as the source of truth for known filenames — no new storage needed.

If a match is found, add a Qdrant `Filter(must=[FieldCondition(key="source",
match=MatchValue(value=matched_filename))])` to the query — this hard-restricts
retrieval to that document's chunks before/alongside vector ranking. This is the most
direct, reliable way to satisfy "if a client-specific detail is asked from a specific
document, retrieve from that document" — it does not depend on vector similarity
alone, which can be fuzzy.

### 4b. Multi-vector hybrid search (extends today's 2-way RRF to 4-way)
Today (`app/rag/retrieve.py`) prefetches `dense` and `sparse`, fuses with
`models.FusionQuery(fusion=models.Fusion.RRF)`. Extend this to prefetch across:
- `dense_content` (existing)
- `sparse_bm25` (existing)
- `dense_summary` (new)
- `dense_metadata` (new)
- `dense_table` (new, harmless to always include — Qdrant simply won't match points
  that lack this vector, per the confirmed partial-vector behavior)

All fused with the same RRF mechanism already in use — this is a natural extension,
not a redesign, of the existing `models.Prefetch(...)` list in `search()`.

### 4c. Reranking (unchanged)
The existing `bge-reranker-base` cross-encoder rerank step in `app/rag/retrieve.py`
stays exactly as-is, applied to the fused top-K from step 4b.

---

## 5. Exact implementation plan (files to change/add)

### `app/config.py`
Add:
```python
rag_multivector_enabled: bool = False   # opt-in until the new collection is validated
rag_table_chunk_threshold: float = 0.4  # fraction of pipe-delimited lines to call a chunk "table-heavy"
qdrant_collection_v2: str = "firstsource_analyst_kb_v2"  # new collection with expanded schema
```
(Keep `qdrant_collection` pointing at the existing collection until migration is verified,
then either rename or switch `QDRANT_COLLECTION` in `.env` to the v2 name.)

### `app/rag/embeddings.py` — add three new functions
```python
def generate_retrieval_proxy(chunk_text: str, settings) -> str:
    """One short LLM call: summary or hypothetical questions, for the dense_summary vector."""
    # Use LLMClient.ask() with a concise prompt. Keep the OUTPUT short (this is what
    # gets embedded for dense_summary) - target 1-3 sentences or 3-5 short questions.

def generate_document_identity_summary(document_name: str, sample_chunks: list[str], settings) -> str:
    """One LLM call per DOCUMENT (not per chunk): source, type, topic, key entities.
    Called once during ingestion of a new document; the resulting text is embedded
    once and copied onto every chunk's dense_metadata vector for that document."""

def is_table_heavy(chunk_text: str, threshold: float) -> bool:
    """Heuristic: fraction of lines containing ' | ' >= threshold."""

def generate_table_paraphrase(chunk_text: str, settings) -> str:
    """One LLM call: turn pipe-delimited rows into a natural-language paraphrase,
    for the dense_table vector. Only called when is_table_heavy() is True."""
```

### `app/rag/index.py`
- `ensure_collection()`: expand `vectors_config` to include `dense_content`,
  `dense_summary`, `dense_metadata`, `dense_table` (all same dim as the embedding
  model, e.g. 3072 for `text-embedding-3-large`) and `sparse_vectors_config` for
  `sparse_bm25`. This collection must be **newly created** (see Migration below) —
  do not try to alter the existing `firstsource_analyst_kb_test` collection in place.
- `index_document()`: for each chunk —
  1. Compute `dense_content` (existing embed call, unchanged)
  2. Compute `sparse_bm25` (existing, unchanged)
  3. Compute `dense_summary` = embed(`generate_retrieval_proxy(chunk.text)`)
  4. Compute `dense_metadata` = embed(the document's cached identity summary —
     generate once per document, reuse for all its chunks, don't regenerate per chunk)
  5. If `is_table_heavy(chunk.text)`: compute `dense_table` =
     embed(`generate_table_paraphrase(chunk.text)`); otherwise omit this vector entirely
     for this point (confirmed supported by Qdrant)
  6. Expand payload with `content_type` and `document_type` fields

### `app/rag/retrieve.py`
- Add `_match_known_source(query: str, settings) -> Optional[str]`: fetches distinct
  known document names (query Postgres `documents` table via `app/rag/store.py`, or
  cache this list), does a simple case-insensitive substring/fuzzy check against the
  query text, returns the matched filename or `None`.
- `search()`: if a source match is found, build a Qdrant `Filter` and pass it to every
  `Prefetch` in the query. Extend the `prefetch` list from 2 entries to up to 5
  (`dense_content`, `sparse_bm25`, `dense_summary`, `dense_metadata`, `dense_table`),
  keep the existing `FusionQuery(fusion=models.Fusion.RRF)` as the top-level query,
  keep the existing rerank step unchanged.

### `app/rag/ingest.py`
- Before the per-chunk loop for a document: call
  `generate_document_identity_summary()` once (using e.g. the first 2-3 chunks'
  text as a representative sample), embed it once, and pass it into `index_document()`
  so every chunk from this document gets the same `dense_metadata` vector value.

### Migration (`app/rag/reindex.py` — new file, or a flag on `ingest.py`)
Because Qdrant collection vector schemas are fixed at creation:
1. Create the new collection (e.g. `firstsource_analyst_kb_v2`) with the expanded
   5-vector schema via `ensure_collection()`.
2. Re-run ingestion against this new collection for every document in the corpus
   (local folder and/or SharePoint, whichever is configured) — this naturally
   populates all 5 vectors per chunk from scratch.
3. Once verified (see validation below), update `QDRANT_COLLECTION` in `.env` to
   point at the new collection. The old collection can be kept for rollback or deleted
   once confidence is established.

---

## 6. Validation plan (how to know it worked)

1. **Live spot-check**: query something that explicitly names a document/entity,
   e.g. *"What does the SaaS Award submission say about the Mortgage Language Model?"*
   — confirm retrieval is correctly narrowed/boosted toward `Saas_Award_2026_HFS.docx`.
2. **Table-question spot-check**: ask a pricing/table-style question (e.g. against
   `pricing-model.xlsx` or `capability-comparison.csv`) and confirm retrieval quality
   holds or improves versus before.
3. **Full eval re-run** (already-built tooling, reuse as-is):
   ```powershell
   python -m tests.eval.generate_dataset
   python -m tests.eval.run_eval
   ```
   Compare the new scorecard against the last recorded baseline (Faithfulness 0.94,
   ContextualRecall 0.94, AnswerRelevancy 0.91, ContextualPrecision 0.81 — with 2
   borderline failures on comparison-style questions). **The specific success signal
   for this phase is ContextualPrecision improving and those 2 borderline failures
   clearing the 0.5 threshold.**
4. Do **not** run the full `pytest` suite automatically — the user has an explicit
   standing instruction to avoid heavy automated test runs to conserve their Copilot
   subscription's token/credit budget. Use `python test.py` (lightweight diagnostic,
   already exists) and the eval scripts above instead. Only run the full `tests/`
   suite if the user explicitly asks.

---

## 7. Explicit non-goals for this phase (do not build these here)
- **ColQwen2.5 / any separate visual embedding model** — user explicitly excluded this.
  Images already flow into `dense_content`/`dense_summary` etc. as GPT-5.4-vision-
  generated text, exactly like any other content. Do not add a visual vector space.
- **Semantic cache** (Postgres query/answer caching) — discussed and deferred earlier
  in the project; not part of this multi-vector work.
- **Scheduled/automatic ingestion** — deferred until real SharePoint access
  (Entra app registration) is wired up; out of scope here.
- **A live per-response quality badge in the UI** — this was discussed as a separate,
  parallel idea (a lightweight self-assessment call shown as a green/orange/red badge
  in the generated dashboard). It is a **different, independent feature** from
  multi-vector retrieval and is not part of this specific plan — implement separately
  if/when the user asks for it.

---

## 8. Known constraints / things to double-check when implementing
- The embedding deployment (`text-embedding-3-large`) is on a **different Azure OpenAI
  resource** (different key+endpoint) than the chat/vision deployment (`gpt-5.4`) —
  see `azure_openai_embedding_*` vs `azure_openai_*` settings in `app/config.py`.
  Use `DenseEmbedder` (embedding resource) for all `dense_*` vectors, and
  `LLMClient`/vision calls (chat resource) for generating the summary/paraphrase TEXT
  that then gets embedded.
- Ingestion will take noticeably longer per document (several extra LLM calls per
  chunk/document) — this is expected and accepted given no quota constraint, but
  mention it when reporting ingestion progress/logs.
- Re-use the existing best-effort, non-fatal error handling pattern used throughout
  `app/rag/` (try/except + log.warning, never raise) for every new LLM call added here
  — a failure to generate a summary/paraphrase for one chunk must not abort the whole
  ingestion run.
- Point IDs must remain deterministic UUID5 (existing pattern in `app/rag/index.py`)
  so re-ingestion updates rather than duplicates points.

---

## 9. Summary checklist for implementation

- [ ] Add config settings (`rag_multivector_enabled`, `rag_table_chunk_threshold`, new collection name)
- [ ] Add `generate_retrieval_proxy`, `generate_document_identity_summary`, `is_table_heavy`, `generate_table_paraphrase` to `app/rag/embeddings.py`
- [ ] Expand `ensure_collection()` schema in `app/rag/index.py` (new collection, 5 dense + 1 sparse vector)
- [ ] Update `index_document()` to compute and upsert all vectors + expanded payload
- [ ] Update `app/rag/ingest.py` to generate the per-document identity summary once
- [ ] Add source-name filter matching + extend prefetch/fusion to 5-way in `app/rag/retrieve.py`
- [ ] Create/re-run a migration path into the new collection
- [ ] Live-test document-specific and table-specific queries
- [ ] Re-run `generate_dataset.py` + `run_eval.py`, compare scorecard to the recorded baseline
- [ ] Update `QDRANT_COLLECTION` in `.env` once validated
