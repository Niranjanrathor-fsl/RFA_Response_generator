"""Shared fixtures. No test ever calls the real Anthropic API."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set before any app module imports its settings.
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com/")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
os.environ.setdefault("AUTH_MODE", "disabled")
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("SESSION_SECRET", "test-secret")

from app.schemas import ResponseDocument  # noqa: E402


SAMPLE_RFI_PAYLOAD = {
    "title": "Acme Bank Analyst RFI Response",
    "subtitle": "Firstsource response covering liability, model strategy and innovation footprint.",
    "metrics": [
        {"value": "32-38", "label": "GenAI engagements", "accent": True, "icon": "chart"},
        {"value": "8", "label": "Innovation labs", "icon": "bulb"},
        {"value": ">99%", "label": "Compliance accuracy", "accent": True},
        {"value": "25+", "label": "Years domain depth", "icon": "clock"},
    ],
    "tabs": [
        {
            "name": "Overview",
            "icon": "doc",
            "intro": "Three questions answered across two source documents.",
            "bullets": ["Kairos delivers Intelligence that Operates.", "Governed Autonomy throughout."],
            "callout": {"title": "Key takeaway", "body": "Outcome accountability is underwritten."},
        },
        {
            "name": "Responses",
            "intro": "Answers preserve the client's original numbering.",
            "qa": [
                {
                    "n": "Q1",
                    "q": "Describe your liability and indemnification model.",
                    "a": "Liability is capped at 12 months of service fees, direct damages only.\n\nFirstsource carries E&O and cyber-liability insurance.",
                },
                {
                    "n": "Q7",
                    "q": "Do you build foundational models?",
                    "a": "Firstsource does not build foundational LLMs. It builds domain-specific models.",
                },
                {
                    "n": "3.2(a)",
                    "q": "What is your innovation lab footprint?",
                    "a": "Eight labs and 95 FTE, with 12+ proprietary solutions in the past 12 months.",
                },
            ],
        },
        {
            "name": "Capabilities",
            "intro": "Portfolio detail.",
            "table": {
                "headers": ["Area", "Detail"],
                "rows": [["Generative AI", "32-38 engagements"], ["Agentic AI", "16-22 engagements"]],
            },
        },
        {
            "name": "Sources",
            "intro": "What each uploaded document contributed.",
            "bullets": ["ClientRFI.docx - supplied the questions.", "Capabilities.pptx - supporting evidence."],
        },
    ],
}

SAMPLE_SUMMARY_PAYLOAD = {
    "title": "Kairos Capability Overview",
    "subtitle": "Summary of the Firstsource Kairos positioning deck.",
    "metrics": [{"value": "5", "label": "Tenets"}, {"value": "4", "label": "Kairos dimensions"}],
    "tabs": [
        {"name": "Overview", "intro": "Intelligence that Operates.", "bullets": ["Domain Intelligence"]},
        {"name": "Architecture", "intro": "Five layers.", "bullets": ["Intelligent Context Framework"]},
        {"name": "Outcomes", "intro": "Underwritten outcomes.", "bullets": ["40% faster decisioning"]},
    ],
}


@pytest.fixture
def rfi_document() -> ResponseDocument:
    return ResponseDocument.model_validate(SAMPLE_RFI_PAYLOAD)


@pytest.fixture
def summary_document() -> ResponseDocument:
    return ResponseDocument.model_validate(SAMPLE_SUMMARY_PAYLOAD)


@pytest.fixture
def client():
    """FastAPI test client.

    Used as a context manager so the lifespan actually runs - that is what
    exercises startup validation of the knowledge base.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
