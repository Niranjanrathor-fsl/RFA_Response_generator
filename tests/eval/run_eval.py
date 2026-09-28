"""Phase 2 evaluation runner.

Run a new evaluation: ``python -m tests.eval.run_eval``
Resume an interrupted PostgreSQL-backed evaluation:
``python -m tests.eval.run_eval --resume-run <run-id>``
Quick smoke test on a small subset: ``python -m tests.eval.run_eval --limit 10``

Questions come from tests/eval/generated_dataset.json - auto-generated coverage
across every indexed document/format. Refresh with
`python -m tests.eval.generate_dataset` whenever documents change.

Generated questions carry an expected_output + source context from the synthesizer,
so all five metrics are scored - the full picture of both retrieval and generation
quality. (The old hand-written questions were dropped: they were answerable only
from the local sample documents, which are not in the SharePoint index.) Anything under its metric's
threshold is listed explicitly in a Failures section at the end.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional, TypeVar

from deepeval.metrics import (
    AnswerRelevancyMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    ContextualRelevancyMetric,
    FaithfulnessMetric,
)
from deepeval.test_case import LLMTestCase

from app.config import get_settings
from app.knowledge import get_knowledge_base
from app.llm import LLMClient
from app.prompts import build_system_prompt
from app.rag import store
from app.rag.retrieve import search

from .judge_model import AzureJudgeModel

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("tests.eval.run_eval")

GENERATED_PATH = Path(__file__).parent / "generated_dataset.json"
RETRY_WINDOW_SECONDS = 180
INITIAL_RETRY_DELAY_SECONDS = 5
MAX_RETRY_DELAY_SECONDS = 30
_T = TypeVar("_T")


class RetryWindowExceeded(RuntimeError):
    """A transient Azure failure continued beyond the configured retry window."""


def _is_transient_network_error(exc: Exception) -> bool:
    """Keep retrying connectivity and service-transient errors, not bad inputs."""
    message = str(exc).lower()
    retry_signals = (
        "could not reach the azure openai api",
        "connection error",
        "connecterror",
        "socket",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "rate limit",
        "returned 429",
        "returned 500",
        "returned 502",
        "returned 503",
        "returned 504",
    )
    return any(signal in message for signal in retry_signals)


def _retry_transient(operation: Callable[[], _T], label: str) -> _T:
    """Retry an Azure-dependent operation through a short network interruption."""
    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
    delay = INITIAL_RETRY_DELAY_SECONDS
    attempt = 1
    while True:
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - selectively retried below
            if not _is_transient_network_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryWindowExceeded(
                    f"{label} was unavailable for {RETRY_WINDOW_SECONDS} seconds."
                ) from exc
            wait_seconds = min(delay, remaining)
            log.warning(
                "%s failed (%s). Retrying in %.0f seconds (attempt %d; %.0f seconds left).",
                label, exc, wait_seconds, attempt, remaining,
            )
            time.sleep(wait_seconds)
            delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)
            attempt += 1


def _load_generated() -> List[dict]:
    if not GENERATED_PATH.exists():
        log.info("No %s found - run `python -m tests.eval.generate_dataset` for broader coverage.",
                  GENERATED_PATH.name)
        return []
    return json.loads(GENERATED_PATH.read_text(encoding="utf-8"))


def _score(
    metric, test_case: LLMTestCase, eval_run_id: Optional[int], question: str,
    settings, failures: List[str],
) -> bool:
    try:
        _retry_transient(
            lambda: metric.measure(test_case),
            f"{metric.__class__.__name__} for the current question",
        )
        log.info("  %s: score=%.2f passed=%s", metric.__class__.__name__, metric.score, metric.success)
        store.record_eval_result(
            eval_run_id, question, metric.__class__.__name__,
            float(metric.score), bool(metric.success), metric.reason or "", settings,
        )
        if not metric.success:
            failures.append(
                f"[{metric.__class__.__name__} score={metric.score:.2f} < threshold={metric.threshold}] "
                f"{question}\n    reason: {metric.reason or '(no reason given)'}"
            )
        return True
    except RetryWindowExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - one metric failing must not stop the run
        log.warning("  %s failed to score: %s", metric.__class__.__name__, exc)
        return False


def run(resume_run_id: Optional[int] = None, limit: Optional[int] = None) -> None:
    settings = get_settings()
    if not settings.rag_enabled:
        log.error("RAG_ENABLED is false. Evaluation needs retrieval to be configured.")
        return

    knowledge = get_knowledge_base()
    system_prompt = build_system_prompt(knowledge, "business (SVP/VP, sales, solutioning)")
    client = LLMClient(settings)
    judge = AzureJudgeModel(settings)

    # These three need only input/actual_output/retrieval_context - no ideal answer.
    baseline_metrics = [
        FaithfulnessMetric(threshold=0.5, model=judge, include_reason=True),
        AnswerRelevancyMetric(threshold=0.5, model=judge, include_reason=True),
        ContextualRelevancyMetric(threshold=0.5, model=judge, include_reason=True),
    ]
    # These two also need an expected_output (ideal answer).
    ground_truth_metrics = baseline_metrics + [
        ContextualPrecisionMetric(threshold=0.5, model=judge, include_reason=True),
        ContextualRecallMetric(threshold=0.5, model=judge, include_reason=True),
    ]

    store.ensure_schema(settings)
    generated = _load_generated()
    generated_items = generated
    if limit is not None:
        generated_items = generated[:limit]
        log.info(
            "--limit %d: evaluating %d question(s) instead of the full set.",
            limit, len(generated_items),
        )
    metric_names = ", ".join(type(m).__name__ for m in ground_truth_metrics)
    if resume_run_id is not None:
        if not settings.pg_enabled:
            log.error("--resume-run requires PG_ENABLED=true.")
            return
        eval_run_id = resume_run_id
        persisted_metrics = store.fetch_eval_result_metrics(eval_run_id, settings)
        log.info(
            "Resuming eval run %s; %d questions already have saved metric results.",
            eval_run_id, len(persisted_metrics),
        )
    else:
        limit_suffix = f" [--limit {limit}]" if limit is not None else ""
        eval_run_id = store.start_eval_run(
            f"Phase 2 scorecard: {len(generated_items)} generated "
            f"questions ({metric_names}){limit_suffix}",
            settings,
        )
        persisted_metrics = {}
        log.info("Started eval run %s.", eval_run_id)
    failures: List[str] = []

    def evaluate(question: str, expected_output: Optional[str], context: Optional[List[str]]) -> None:
        metrics = ground_truth_metrics if expected_output else baseline_metrics
        completed_metric_names = persisted_metrics.get(question, set())
        missing_metrics = [
            metric for metric in metrics if type(metric).__name__ not in completed_metric_names
        ]
        if not missing_metrics:
            log.info("Skipping already-scored question: %s", question)
            return

        log.info("Question: %s", question)
        chunks = search(question, settings)
        retrieval_context = [c.text for c in chunks] or ["(no grounding retrieved)"]
        answer = _retry_transient(
            lambda: client.ask(system_prompt, question + "\n\nAnswer in 3-6 sentences."),
            "Azure OpenAI answer generation",
        )

        test_case = LLMTestCase(
            input=question,
            actual_output=answer,
            retrieval_context=retrieval_context,
            expected_output=expected_output,
            context=context,
        )
        for metric in missing_metrics:
            if _score(metric, test_case, eval_run_id, question, settings, failures):
                persisted_metrics.setdefault(question, set()).add(type(metric).__name__)

    try:
        for item in generated_items:
            evaluate(item["input"], item.get("expected_output"), item.get("context"))
    except RetryWindowExceeded as exc:
        log.error(
            "%s The run was left unfinished without losing saved results. "
            "After connectivity returns, resume with: "
            "python -m tests.eval.run_eval --resume-run %s",
            exc, eval_run_id,
        )
        return

    store.finish_eval_run(eval_run_id, settings)

    scorecard = store.fetch_eval_scorecard(eval_run_id, settings) if eval_run_id else {}
    print("\n--- Scorecard ---")
    if scorecard:
        for metric, values in scorecard.items():
            print(f"{metric:22s} avg_score={values['avg_score']:.2f}  pass_rate={values['pass_rate']:.0%}")
    else:
        print("(PG_ENABLED is false - scorecard not persisted; see per-question logs above.)")

    print(f"\n--- Failures (below threshold): {len(failures)} ---")
    for line in failures:
        print(f"  {line}")
    if not failures:
        print("  (none)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run or resume the Phase 2 evaluation.")
    parser.add_argument(
        "--resume-run", type=int, metavar="RUN_ID",
        help="Reuse this PostgreSQL run and skip metric results already saved for each question.",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N",
        help="Only evaluate the first N questions total - for a quick smoke test.",
    )
    args = parser.parse_args()
    run(resume_run_id=args.resume_run, limit=args.limit)
