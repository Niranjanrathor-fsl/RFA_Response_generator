"""Question detection in uploaded documents, and per-question retrieval."""

from __future__ import annotations

from app.config import get_settings
from app.questions import DetectedQuestion, detect_questions, understand_questions
from app.rag import retrieve
from app.rag.retrieve import RetrievedChunk, format_question_grounding, search_per_question


def _texts(questions):
    return [q.text for q in questions]


# ---------------------------------------------------------------- detection
def test_numbered_questions_are_detected_with_labels_stripped():
    text = (
        "Firstsource RFI 2026\n"
        "Q1. Describe your liability cap and insurance coverage.\n"
        "Q2) How many GenAI engagements are in production?\n"
        "3. Provide details of your innovation labs.\n"
        "1.2 Outline the governance model for autonomous agents.\n"
    )
    assert _texts(detect_questions(text)) == [
        "Describe your liability cap and insurance coverage.",
        "How many GenAI engagements are in production?",
        "Provide details of your innovation labs.",
        "Outline the governance model for autonomous agents.",
    ]


def test_explicit_q_labels_count_even_without_a_question_mark():
    # Real analyst RFIs label topic-style questions: no "?" and no request verb.
    text = (
        "Question 1: Contractual Liability, Indemnification & Risk-Sharing for AI Engagements\n"
        "Q1. Why This Matters - What Problem We Are Solving\n"
        "1. Best Startup / Emerging Firm\n"
    )
    assert _texts(detect_questions(text)) == [
        "Contractual Liability, Indemnification & Risk-Sharing for AI Engagements",
        "Why This Matters - What Problem We Are Solving",
    ]


def test_unnumbered_question_marks_and_imperatives_are_detected():
    text = (
        "Do you build foundational large language models?\n"
        "Please explain your approach to responsible AI in regulated industries.\n"
    )
    assert _texts(detect_questions(text)) == [
        "Do you build foundational large language models?",
        "Please explain your approach to responsible AI in regulated industries.",
    ]


def test_questions_inside_spreadsheet_rows_are_detected():
    text = (
        "--- Sheet: Questionnaire ---\n"
        "ID | Question | Response\n"
        "A1 | What is your total headcount in India? | \n"
        "A2 | Describe your pricing models for AI services. | \n"
    )
    assert _texts(detect_questions(text)) == [
        "What is your total headcount in India?",
        "Describe your pricing models for AI services.",
    ]


def test_plain_prose_headings_and_markers_are_not_questions():
    text = (
        "===== SOURCE DOCUMENT 1 of 2: deck.pptx =====\n"
        "--- Slide 3 ---\n"
        "What we do\n"
        "Firstsource delivers intelligent operations for global enterprises.\n"
        "List of clients\n"
        "1. Revenue grew 12% year over year.\n"
    )
    assert _texts(detect_questions(text)) == []


def test_duplicates_are_removed_and_the_limit_applies():
    text = "\n".join(["Q1. What is your headcount?", "What is your headcount?"]
                     + [f"Q{i}. Describe capability number {i} in detail." for i in range(2, 100)])
    questions = detect_questions(text, max_questions=10)
    assert questions[0].text == "What is your headcount?"
    assert len(questions) == 10


# ---------------------------------------------------------------- retrieval
def _chunk(text, source="doc.docx"):
    return RetrievedChunk(text=text, source=source, location="Page 1", web_url="", score=1.0)


def test_each_question_gets_its_own_search(monkeypatch):
    asked = []

    def fake_search(query, settings):
        asked.append((query, settings.rag_rerank_top_k))
        return [_chunk(f"answer to {query}")]

    monkeypatch.setattr(retrieve, "search", fake_search)
    settings = get_settings().model_copy(update={"rag_per_question_top_k": 3})

    results = search_per_question(["Q one?", "Q two?"], settings)

    assert [q for q, _ in results] == ["Q one?", "Q two?"]
    assert sorted(asked) == [("Q one?", 3), ("Q two?", 3)]
    assert results[1][1][0].text == "answer to Q two?"


def test_grounding_is_grouped_by_question_and_shared_passages_printed_once():
    shared = _chunk("Liability is capped at 12 months of fees.", "contract.docx")
    block = format_question_grounding([
        ("Describe your liability cap.", [shared]),
        ("What insurance do you hold?", [shared, _chunk("E&O cover of $10M.", "insurance.pdf")]),
        ("Unanswerable?", []),
    ])

    assert block.count("Liability is capped at 12 months of fees.") == 1
    assert "Describe your liability cap." in block
    assert "E&O cover of $10M." in block
    assert "see passage above" in block
    assert "no matching passages" in block


def test_no_questions_means_no_grounding_block():
    assert format_question_grounding([]) == ""


# ---------------------------------------------------------------- location tags
def test_each_question_is_tagged_with_its_file_and_page():
    text = (
        "===== SOURCE DOCUMENT 1 of 2: survey.xlsx =====\n"
        "--- Sheet: Delivery ---\n"
        "A1 | What is your total headcount in India? |\n"
        "===== SOURCE DOCUMENT 2 of 2: rfi.pdf =====\n"
        "--- Page 3 ---\n"
        "Q4. Describe your pricing models for AI services.\n"
    )
    assert [(q.text, q.location) for q in detect_questions(text)] == [
        ("What is your total headcount in India?", "survey.xlsx, Sheet: Delivery"),
        ("Describe your pricing models for AI services.", "rfi.pdf, Page 3"),
    ]


def test_a_single_upload_uses_the_given_file_name():
    questions = detect_questions("--- Slide 2 ---\nQ1. What is your headcount?", source_name="deck.pptx")
    assert questions[0].location == "deck.pptx, Slide 2"


def test_search_query_defaults_to_the_question_itself():
    assert detect_questions("Q1. What is your headcount?")[0].search_query == "What is your headcount?"


# ---------------------------------------------------------------- understanding
class _FakeLLM:
    reply = ""
    prompts = []

    def __init__(self, settings=None):
        pass

    def ask(self, system_prompt, user_prompt):
        type(self).prompts.append(user_prompt)
        return type(self).reply


def _understand(monkeypatch, reply, questions, corpus="Avasant Applied AI Services RadarView 2026 survey"):
    import app.questions as questions_module

    _FakeLLM.reply, _FakeLLM.prompts = reply, []
    monkeypatch.setattr(questions_module, "LLMClient", _FakeLLM)
    return understand_questions(questions, corpus, get_settings())


def test_understanding_rewrites_each_question_into_a_standalone_search_query(monkeypatch):
    questions = [DetectedQuestion("Why This Matters - What Problem We Are Solving", "hfs.docx"),
                 DetectedQuestion("What is your headcount?", "rfi.pdf, Page 2")]
    reply = """{"questions": [
      {"i": 1, "query": "Firstsource services-as-software transformation business problem HFS award",
       "type": "narrative", "entities": ["HFS", "Services-as-Software"]},
      {"i": 2, "query": "Firstsource total employee headcount", "type": "fact", "entities": []}
    ]}"""

    result = _understand(monkeypatch, reply, questions)

    assert result[0].text == "Why This Matters - What Problem We Are Solving"
    assert result[0].search_query == "Firstsource services-as-software transformation business problem HFS award"
    assert result[0].kind == "narrative"
    assert result[0].entities == ["HFS", "Services-as-Software"]
    assert result[1].search_query == "Firstsource total employee headcount"
    # The model sees the document's context and every question with its location.
    assert "Avasant Applied AI Services" in _FakeLLM.prompts[0]
    assert "rfi.pdf, Page 2" in _FakeLLM.prompts[0]


def test_understanding_failure_keeps_the_original_questions(monkeypatch):
    questions = [DetectedQuestion("What is your headcount?", "")]
    result = _understand(monkeypatch, "not json at all", questions)
    assert result[0].search_query == "What is your headcount?"


def test_understanding_ignores_blank_or_unknown_entries(monkeypatch):
    questions = [DetectedQuestion("What is your headcount?", ""), DetectedQuestion("Describe your labs.", "")]
    reply = '{"questions": [{"i": 1, "query": "  "}, {"i": 9, "query": "stray"}, {"i": 2, "query": "Firstsource innovation labs"}]}'
    result = _understand(monkeypatch, reply, questions)
    assert [q.search_query for q in result] == ["What is your headcount?", "Firstsource innovation labs"]


def test_understanding_is_skipped_when_disabled(monkeypatch):
    import app.questions as questions_module

    monkeypatch.setattr(questions_module, "LLMClient",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no LLM call expected")))
    settings = get_settings().model_copy(update={"rag_query_understanding_enabled": False})
    questions = [DetectedQuestion("What is your headcount?", "")]
    assert understand_questions(questions, "corpus", settings) == questions
