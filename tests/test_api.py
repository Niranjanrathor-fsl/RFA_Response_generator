"""End-to-end API behaviour with the model call stubbed.

These tests prove the full request path works - upload, extraction, corpus
assembly, prompt build, validation and rendering - without spending a token.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.routes import generate as generate_route
from tests.conftest import SAMPLE_RFI_PAYLOAD, SAMPLE_SUMMARY_PAYLOAD


class StubLLMClient:
    """Drop-in replacement for LLMClient that records what it was asked."""

    payload = SAMPLE_RFI_PAYLOAD
    last_system_prompt = ""
    last_user_prompt = ""

    def __init__(self, settings=None):
        self.settings = settings

    @property
    def model(self):
        return "stub-model"

    def generate_document(self, system_prompt, user_prompt):
        type(self).last_system_prompt = system_prompt
        type(self).last_user_prompt = user_prompt
        return self.payload


@pytest.fixture
def stub_llm(monkeypatch):
    StubLLMClient.payload = SAMPLE_RFI_PAYLOAD
    monkeypatch.setattr(generate_route, "LLMClient", StubLLMClient)
    return StubLLMClient


def docx_bytes(text: str) -> bytes:
    from docx import Document

    document = Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------ meta routes
def test_healthz_reports_the_knowledge_base(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["knowledge_base_loaded"] is True
    assert body["knowledge_base_chars"] > 10_000


def test_client_config_lists_formats_but_no_secrets(client):
    body = client.get("/api/config").json()
    assert set(body["formats"]) == {"dashboard", "qa", "docx", "pptx", "xlsx"}
    assert ".pdf" in body["accepted_extensions"]
    assert "azure_openai_api_key" not in json.dumps(body).lower()


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "RFP Response Generator" in response.text
    # The Cowork bridge must be gone.
    assert "cowork" not in response.text.lower()


def test_frontend_never_references_the_cowork_bridge():
    from pathlib import Path

    from app.config import get_settings

    app_js = (get_settings().static_dir / "js" / "app.js").read_text(encoding="utf-8")
    assert "askClaude" not in app_js
    assert "window.cowork." not in app_js
    assert "/api/generate" in app_js
    assert "api.anthropic.com" not in app_js  # browser never talks to Anthropic


# -------------------------------------------------------------------- generate
def test_generate_from_pasted_text(client, stub_llm):
    response = client.post(
        "/api/generate",
        data={"pasted": "Q1. Describe your liability model.", "audience": "business"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "rfi"
    assert body["question_count"] == 3
    assert [s["name"] for s in body["sources"]] == ["Pasted content"]
    assert body["model"] == "stub-model"


def test_generate_requires_some_content(client, stub_llm):
    response = client.post("/api/generate", data={"pasted": "   "})
    assert response.status_code == 400
    assert "at least one document" in response.json()["detail"]


def test_generate_from_multiple_files_merges_into_one_response(client, stub_llm):
    files = [
        ("files", ("ClientRFI.docx", docx_bytes("Q1. Describe your liability model."),
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ("files", ("Capabilities.txt", b"Kairos delivers Intelligence that Operates.", "text/plain")),
    ]
    response = client.post("/api/generate", files=files, data={"pasted": "Extra context note."})
    assert response.status_code == 200
    body = response.json()

    names = [s["name"] for s in body["sources"]]
    assert names == ["ClientRFI.docx", "Capabilities.txt", "Pasted content"]

    prompt = stub_llm.last_user_prompt
    assert "MULTIPLE SOURCE DOCUMENTS (3)" in prompt
    assert "===== SOURCE DOCUMENT 1 of 3: ClientRFI.docx =====" in prompt
    assert "===== SOURCE DOCUMENT 3 of 3: Pasted content =====" in prompt
    assert "SUPPORTING EVIDENCE" in prompt


def test_single_source_prompt_has_no_multi_document_section(client, stub_llm):
    client.post("/api/generate", data={"pasted": "Just one source."})
    assert "MULTIPLE SOURCE DOCUMENTS" not in stub_llm.last_user_prompt
    assert "SOURCE DOCUMENT 1 of" not in stub_llm.last_user_prompt


def test_every_detected_question_is_searched_separately(client, stub_llm, monkeypatch):
    searched = []

    def fake_per_question(questions, settings=None):
        searched.extend(questions)
        return [(q, []) for q in questions]

    monkeypatch.setattr(generate_route, "search_per_question", fake_per_question)
    # Past the old 8,000-character single-query window.
    filler = "Background context about the programme. " * 250
    client.post("/api/generate", data={
        "pasted": f"Q1. Describe your governance model.\n{filler}\nQ2. What is your headcount?"
    })

    assert searched == ["Describe your governance model.", "What is your headcount?"]


def test_input_without_questions_falls_back_to_one_search(client, stub_llm, monkeypatch):
    single = []
    monkeypatch.setattr(generate_route, "search_per_question",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("not expected")))
    monkeypatch.setattr(generate_route, "rag_search", lambda q, *a: single.append(q) or [])

    client.post("/api/generate", data={"pasted": "Firstsource delivers intelligent operations."})

    assert single == ["Firstsource delivers intelligent operations."]


def test_knowledge_base_is_injected_server_side(client, stub_llm):
    client.post("/api/generate", data={"pasted": "Q1. Anything?"})
    system_prompt = stub_llm.last_system_prompt
    assert "ANALYST KNOWLEDGE BASE" in system_prompt
    assert "Intelligence that Operates" in system_prompt
    assert "DEPRECATED TERMS" in system_prompt


def test_duplicate_uploads_are_ignored(client, stub_llm):
    payload = b"Q1. Duplicate content."
    files = [
        ("files", ("same.txt", payload, "text/plain")),
        ("files", ("same.txt", payload, "text/plain")),
    ]
    body = client.post("/api/generate", files=files).json()
    assert len(body["sources"]) == 1


def test_unreadable_scanned_pdf_is_reported_clearly(client, stub_llm):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    response = client.post(
        "/api/generate",
        files=[("files", ("scan.pdf", buffer.getvalue(), "application/pdf"))],
    )
    assert response.status_code == 400
    assert "No readable text" in response.json()["detail"]


def test_legacy_doc_upload_is_rejected_with_guidance(client, stub_llm):
    response = client.post(
        "/api/generate",
        files=[("files", ("old.doc", b"\xd0\xcf\x11\xe0legacy", "application/msword"))],
    )
    assert response.status_code == 400
    assert "legacy" in response.json()["detail"].lower()


def test_too_many_files_rejected(client, stub_llm, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_files", 2)
    files = [("files", (f"f{i}.txt", b"content", "text/plain")) for i in range(3)]
    response = client.post("/api/generate", files=files)
    assert response.status_code == 413
    assert "Too many files" in response.json()["detail"]


def test_oversized_file_rejected(client, stub_llm, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
    response = client.post(
        "/api/generate", files=[("files", ("big.txt", b"x" * 2048, "text/plain"))]
    )
    assert response.status_code == 413


def test_title_override_wins(client, stub_llm):
    body = client.post(
        "/api/generate", data={"pasted": "Q1. Anything?", "title": "My Custom Title"}
    ).json()
    assert body["document"]["title"] == "My Custom Title"


def test_model_failure_surfaces_as_502(client, monkeypatch):
    from app.llm import LLMError

    class FailingClient(StubLLMClient):
        def generate_document(self, system_prompt, user_prompt):
            raise LLMError("Azure OpenAI API returned 529: overloaded")

    monkeypatch.setattr(generate_route, "LLMClient", FailingClient)
    response = client.post("/api/generate", data={"pasted": "Q1. Anything?"})
    assert response.status_code == 502
    assert "overloaded" in response.json()["detail"]


def test_unrenderable_model_output_surfaces_as_502(client, monkeypatch):
    class BadShapeClient(StubLLMClient):
        def generate_document(self, system_prompt, user_prompt):
            return {"title": "Broken", "tabs": "this should be a list"}

    monkeypatch.setattr(generate_route, "LLMClient", BadShapeClient)
    response = client.post("/api/generate", data={"pasted": "Q1. Anything?"})
    assert response.status_code == 502


def test_summary_mode_detected_when_no_questions(client, monkeypatch):
    class SummaryClient(StubLLMClient):
        payload = SAMPLE_SUMMARY_PAYLOAD

    monkeypatch.setattr(generate_route, "LLMClient", SummaryClient)
    body = client.post("/api/generate", data={"pasted": "A capability deck."}).json()
    assert body["mode"] == "summary"
    assert body["question_count"] == 0


# ---------------------------------------------------------------------- render
@pytest.mark.parametrize(
    "fmt,expected",
    [
        ("dashboard", "text/html"),
        ("qa", "text/html"),
        ("docx", "wordprocessingml"),
        ("pptx", "presentationml"),
        ("xlsx", "spreadsheetml"),
    ],
)
def test_render_each_format(client, stub_llm, fmt, expected):
    generated = client.post("/api/generate", data={"pasted": "Q1. Anything?"}).json()
    response = client.post(
        f"/api/render/{fmt}",
        json={"document": generated["document"], "sources": generated["sources"]},
    )
    assert response.status_code == 200
    assert expected in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["X-Output-Filename"]
    assert len(response.content) > 1000
    if fmt in {"docx", "pptx", "xlsx"}:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None


def test_render_rejects_unknown_format(client, stub_llm):
    generated = client.post("/api/generate", data={"pasted": "Q1. Anything?"}).json()
    response = client.post("/api/render/pdf", json={"document": generated["document"]})
    assert response.status_code == 400
    assert "Unknown format" in response.json()["detail"]


def test_render_is_stateless_and_repeatable(client, stub_llm):
    """A second render of the same document must work without another model call."""
    generated = client.post("/api/generate", data={"pasted": "Q1. Anything?"}).json()
    body = {"document": generated["document"], "sources": generated["sources"]}
    first = client.post("/api/render/dashboard", json=body)
    second = client.post("/api/render/dashboard", json=body)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content


def test_render_validates_the_document_body(client):
    response = client.post("/api/render/dashboard", json={"document": {"tabs": "nope"}})
    assert response.status_code == 422


def test_hidden_attribute_is_enforced_in_css():
    """The login gate toggles `hidden` on #app, but `.app{display:grid}` is a
    class selector and outranks the browser's default [hidden]{display:none}.
    Without an explicit rule the app renders straight through the sign-in card.
    """
    from app.config import get_settings

    css = (get_settings().static_dir / "css" / "app.css").read_text(encoding="utf-8")
    assert "[hidden]" in css
    assert "display:none!important" in css.replace(" ", "")
