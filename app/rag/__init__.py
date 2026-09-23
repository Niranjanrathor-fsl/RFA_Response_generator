"""Phase 1 RAG: ground generation in SharePoint (or local test) documents.

Pipeline: sources -> chunking -> embeddings -> Qdrant index -> hybrid retrieval
-> rerank. Disabled by default (RAG_ENABLED=false) so the rest of the app is
unaffected until this is configured.
"""
