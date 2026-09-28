"""Retrieval-only tuning: compare passage-count settings on the eval questions.

ContextualRelevancy, ContextualPrecision and ContextualRecall judge only the
question, the expected answer and the retrieved passages - never the generated
answer. So settings that change WHICH passages come back can be compared without
generating a single answer, at roughly a third of the cost of a full eval.

Each question is searched once, keeping the most passages any setting needs;
every setting is then applied to that same ranked list, so the comparison sees
identical candidates. A passage set that two settings share is judged once.
Progress is saved after every judgement - re-running resumes where it stopped.

    python -m tests.eval.tune_retrieval                      # default settings
    python -m tests.eval.tune_retrieval --configs 5:4,5:3,6:3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Tuple

from deepeval.metrics import (
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    ContextualRelevancyMetric,
)
from deepeval.test_case import LLMTestCase

from app.config import get_settings
from app.rag.retrieve import search

from .judge_model import AzureJudgeModel
from .run_eval import RetryWindowExceeded, _load_generated, _retry_transient

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("tests.eval.tune_retrieval")

RESULTS_PATH = Path(__file__).parent / "tuning_results.json"
METRICS = (ContextualRelevancyMetric, ContextualPrecisionMetric, ContextualRecallMetric)
DEFAULT_CONFIGS = "8:6,5:4,5:3,6:3"  # 8:6 is the current setting


def _parse_configs(text: str) -> List[Tuple[int, float]]:
    configs = []
    for part in text.split(","):
        top_k, margin = part.split(":")
        configs.append((int(top_k), float(margin)))
    return configs


def _apply(ranked: List[Tuple[float, str]], top_k: int, margin: float) -> List[str]:
    """The same selection retrieve.py makes: best top_k, then drop the weak tail."""
    top = ranked[:top_k]
    if not top:
        return []
    best = top[0][0]
    return [text for score, text in top if score >= best - margin]


def _key(question: str, texts: List[str]) -> str:
    return hashlib.sha256(json.dumps([question, texts]).encode("utf-8")).hexdigest()


def _load() -> Dict:
    if RESULTS_PATH.exists():
        return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    return {"retrieval": {}, "scores": {}}


def _save(state: Dict) -> None:
    RESULTS_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _retrieve(question: str, settings) -> List[Tuple[float, str]]:
    # search() swallows connection errors and returns [] - indistinguishable from
    # "nothing found" - so an empty result is retried before it is believed.
    for attempt in range(6):
        chunks = search(question, settings)
        if chunks:
            return [(c.score, c.text) for c in chunks]
        log.warning("Empty retrieval (attempt %d/6), retrying in 20s: %s", attempt + 1, question[:80])
        time.sleep(20)
    return []


def run(configs: List[Tuple[int, float]], limit: int = 0) -> None:
    base = get_settings()
    # One search per question, wide enough for every setting and with no tail cut;
    # the cache is bypassed so nothing stale is reused.
    wide = base.model_copy(update={
        "rag_rerank_top_k": max(k for k, _ in configs),
        "rag_rerank_score_margin": 1e9,
        "rag_cache_enabled": False,
    })
    judge = AzureJudgeModel(base)
    items = _load_generated()
    if limit:
        items = items[:limit]
    state = _load()

    try:
        for n, item in enumerate(items, start=1):
            question = item["input"]
            if question not in state["retrieval"]:
                state["retrieval"][question] = _retrieve(question, wide)
                _save(state)
            ranked = state["retrieval"][question]

            for top_k, margin in configs:
                texts = _apply(ranked, top_k, margin)
                key = _key(question, texts)
                done = state["scores"].setdefault(key, {})
                case = LLMTestCase(
                    input=question, actual_output="(not used)",
                    expected_output=item.get("expected_output"),
                    retrieval_context=texts or ["(no grounding retrieved)"],
                )
                for metric_cls in METRICS:
                    name = metric_cls.__name__
                    if name in done:
                        continue
                    metric = metric_cls(threshold=0.5, model=judge, include_reason=False)
                    _retry_transient(lambda: metric.measure(case), f"{name} for question {n}")
                    done[name] = float(metric.score)
                    _save(state)
            log.info("Question %d/%d scored for all settings.", n, len(items))
    except RetryWindowExceeded as exc:
        log.error("%s Progress is saved - re-run the same command to resume.", exc)
        return

    _report(items, configs, state)


def _report(items: List[dict], configs: List[Tuple[int, float]], state: Dict) -> None:
    print(f"\n--- Retrieval tuning ({len(items)} questions) ---")
    print(f"{'setting':14s} {'passages':>8s} " + " ".join(f"{m.__name__[10:-6]:>22s}" for m in METRICS))
    for top_k, margin in configs:
        totals = {m.__name__: [] for m in METRICS}
        counts = []
        for item in items:
            question = item["input"]
            texts = _apply(state["retrieval"].get(question, []), top_k, margin)
            counts.append(len(texts))
            for name, score in state["scores"].get(_key(question, texts), {}).items():
                totals[name].append(score)
        cells = []
        for m in METRICS:
            scores = totals[m.__name__]
            if scores:
                passed = sum(s >= 0.5 for s in scores) / len(scores)
                cells.append(f"{sum(scores) / len(scores):.2f} / {passed:4.0%} (n={len(scores)})")
            else:
                cells.append("-")
        print(f"top {top_k}, gap {margin:<4g} {sum(counts) / len(counts):8.1f} " + " ".join(f"{c:>22s}" for c in cells))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--configs", default=DEFAULT_CONFIGS,
                        help=f"Comma-separated top_k:margin pairs (default {DEFAULT_CONFIGS}).")
    parser.add_argument("--limit", type=int, default=0, help="Only the first N questions (smoke test).")
    args = parser.parse_args()
    run(_parse_configs(args.configs), args.limit)
