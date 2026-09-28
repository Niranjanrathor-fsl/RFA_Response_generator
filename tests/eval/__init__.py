"""Manual DeepEval harness (Phase 2). Never auto-run by pytest.

Run manually:   python -m tests.eval.run_eval

Evaluates retrieval + generation quality against a small labeled question set
and prints a scorecard. If PG_ENABLED=true, results are also persisted to
Postgres (eval_runs / eval_results) via app.rag.store.
"""
