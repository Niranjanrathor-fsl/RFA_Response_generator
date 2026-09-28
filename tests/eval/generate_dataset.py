"""Auto-generates a broad, ever-growing evaluation set from whatever documents
are currently in the corpus - PDF, Word, PowerPoint, Excel, CSV, JSON, text, all
handled the same way generation is (reuses app.rag.chunking.extract_sections,
the exact same multi-format parser used for real ingestion).

Whenever new documents are added, just re-run this to refresh coverage - no one
has to hand-write new questions every time:

    python -m tests.eval.generate_dataset

Output: tests/eval/generated_dataset.json, loaded automatically by run_eval.py
(the only question source it uses).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from deepeval.synthesizer import Synthesizer

from app.config import get_settings
from app.rag.chunking import extract_sections
from app.rag import store
from app.rag.sources import get_document_source

from .judge_model import AzureJudgeModel

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("tests.eval.generate_dataset")

OUTPUT_PATH = Path(__file__).parent / "generated_dataset.json"
# Cap sections per document so the synthesizer prompt stays a reasonable size -
# a document with more pages/slides/sheets than this is still sampled fairly.
MAX_SECTIONS_PER_DOCUMENT = 12


def run(max_goldens_per_document: int = 3, max_documents: int = 0) -> None:
    settings = get_settings()
    if not settings.rag_enabled:
        log.error("RAG_ENABLED is false. Configure ingestion before generating an eval set.")
        return

    source = get_document_source(settings)

    # Only generate questions from documents that are ACTUALLY INDEXED. Asking
    # about a document the retriever cannot see measures ingestion progress, not
    # retrieval quality, and every such question fails for the wrong reason.
    indexed = {
        item_id for item_id, state in store.get_document_states(settings).items()
        if state.content_tag
    }
    if indexed:
        log.info("Restricting synthesis to the %d indexed document(s).", len(indexed))

    batch = source.fetch_changes(None)
    try:
        contexts: List[List[str]] = []
        source_files: List[str] = []
        candidates = [d for d in batch.documents if not indexed or d.item_id in indexed]
        # The listing is grouped by folder, so "the first N" covered one analyst
        # firm only. Sample evenly across the whole listing instead.
        if max_documents and len(candidates) > max_documents:
            step = len(candidates) / max_documents
            candidates = [candidates[int(i * step)] for i in range(max_documents)]
        for document in candidates:
            try:
                sections = extract_sections(document.name, document.fetch())
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                log.warning("Skipping %s: %s", document.name, exc)
                continue
            texts = [text for _, text in sections if text.strip()]
            if not texts:
                log.info("No text extracted from %s, skipping.", document.name)
                continue
            contexts.append(texts[:MAX_SECTIONS_PER_DOCUMENT])
            source_files.append(document.name)
            log.info("Prepared %s: %d section(s) for synthesis.", document.name, len(texts))
    finally:
        source.close()

    if not contexts:
        log.error("No documents found to generate questions from.")
        return

    judge = AzureJudgeModel(settings)
    synthesizer = Synthesizer(model=judge)
    goldens = synthesizer.generate_goldens_from_contexts(
        contexts=contexts,
        include_expected_output=True,
        max_goldens_per_context=max_goldens_per_document,
        source_files=source_files,
    )

    payload = [
        {
            "input": g.input,
            "expected_output": g.expected_output,
            "context": g.context,
            "source_file": g.source_file,
        }
        for g in goldens
    ]
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        "Generated %d question(s) across %d document(s) -> %s",
        len(payload), len(contexts), OUTPUT_PATH,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate an evaluation set from indexed documents.")
    parser.add_argument("--per-document", type=int, default=3,
                        help="Questions to synthesise per document (default 3).")
    parser.add_argument("--max-documents", type=int, default=0,
                        help="Sample at most this many documents (0 = all indexed).")
    args = parser.parse_args()
    run(max_goldens_per_document=args.per_document, max_documents=args.max_documents)
