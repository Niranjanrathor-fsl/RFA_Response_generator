"""Near-duplicate suppression at retrieval time.

The corpus holds several drafts of the same document (v1/v2 workbooks, a deck
and its PDF export). Their passages are near-identical, so without suppression
one question retrieves the same passage several times and crowds out others.
"""

from __future__ import annotations

from app.rag.retrieve import _drop_near_duplicates


def _ranked(*texts):
    # (score, (text, point)) in descending score order - the shape used in retrieve.py.
    return [(10.0 - i, (text, f"point-{i}")) for i, text in enumerate(texts)]


PASSAGE = "Firstsource delivers GenAI across 32-38 engagements with Kairos OS governance."


def test_exact_copy_from_a_sibling_draft_is_dropped_and_the_next_passage_fills_its_slot():
    ranked = _ranked(PASSAGE, PASSAGE, "Innovation labs in Mumbai and Manila.", "Pricing is outcome-based.")

    kept = _drop_near_duplicates(ranked, top_k=3, threshold=0.8)

    assert [text for _, (text, _) in kept] == [
        PASSAGE, "Innovation labs in Mumbai and Manila.", "Pricing is outcome-based.",
    ]


def test_formatting_differences_between_pdf_and_pptx_exports_still_count_as_duplicates():
    pptx = "Agenda\nIntroductions\nAI Strategy\nCase Studies\nQ&A"
    pdf = "AGENDA  Introductions -  AI Strategy -  Case Studies -  Q & A"
    kept = _drop_near_duplicates(_ranked(pptx, pdf), top_k=5, threshold=0.8)
    assert len(kept) == 1


def test_the_highest_ranked_copy_is_the_one_kept():
    kept = _drop_near_duplicates(_ranked(PASSAGE, PASSAGE), top_k=5, threshold=0.8)
    assert kept[0][1][1] == "point-0"


def test_related_but_distinct_passages_are_kept():
    a = "Headcount: 30,000 employees across India, the Philippines, the UK and the US."
    b = "Revenue: USD 950 million in FY26, with healthcare the largest vertical."
    assert len(_drop_near_duplicates(_ranked(a, b), top_k=5, threshold=0.8)) == 2


def test_threshold_of_one_disables_suppression():
    kept = _drop_near_duplicates(_ranked(PASSAGE, PASSAGE), top_k=5, threshold=1.0)
    assert len(kept) == 2
