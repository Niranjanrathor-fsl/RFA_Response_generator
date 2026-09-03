"""Prompt construction: terminology rules, mode rules, multi-document rules."""

from __future__ import annotations

from app.knowledge import load_knowledge_base
from app.prompts import build_system_prompt, build_user_prompt, multi_document_rules


def test_system_prompt_carries_the_knowledge_base_and_brand_rules():
    prompt = build_system_prompt(load_knowledge_base(), "business (SVP/VP)")
    assert "Intelligence that Operates" in prompt
    assert "AUTHORITATIVE" in prompt
    assert "business (SVP/VP)" in prompt
    # Deprecated terms must be named as deprecated, so the model recasts them.
    assert "DEPRECATED TERMS" in prompt
    for term in ("relAI", "Agentic OS", "UnBPO"):
        assert term in prompt


def test_single_source_omits_multi_document_rules():
    assert multi_document_rules(["only.docx"]) == ""
    prompt = build_user_prompt("Some content", ["only.docx"])
    assert "MULTIPLE SOURCE DOCUMENTS" not in prompt


def test_multi_document_rules_name_every_file_and_state_the_policy():
    rules = multi_document_rules(["ClientRFI.docx", "Capabilities.pptx", "Pasted content"])
    assert "MULTIPLE SOURCE DOCUMENTS (3)" in rules
    assert "ClientRFI.docx; Capabilities.pptx; Pasted content" in rules
    for requirement in ("ATTRIBUTE", "DEDUPLICATE", "RECONCILE CONFLICTS", "Sources"):
        assert requirement in rules
    assert "SUPPORTING EVIDENCE" in rules


def test_user_prompt_includes_mode_and_structure_rules_and_the_corpus():
    prompt = build_user_prompt("QUESTION ONE", ["a.docx", "b.pptx"], title_hint="Acme RFI")
    assert "MULTIPLE SOURCE DOCUMENTS (2)" in prompt
    assert "Detect questions GENEROUSLY" in prompt
    assert '"tabs"' in prompt
    assert "Acme RFI" in prompt
    assert "QUESTION ONE" in prompt


def test_rfi_mode_demands_original_numbering_be_preserved():
    prompt = build_user_prompt("Q1. Something?", ["rfi.docx"])
    assert "ORIGINAL number" in prompt
    assert "3.2(a)" in prompt
