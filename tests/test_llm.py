"""JSON extraction from real-world-messy model output."""

from __future__ import annotations

from app.llm import extract_json


def test_plain_json():
    assert extract_json('{"title":"A","tabs":[]}')["title"] == "A"


def test_fenced_json():
    raw = '```json\n{"title":"Fenced","tabs":[]}\n```'
    assert extract_json(raw)["title"] == "Fenced"


def test_json_with_leading_prose():
    raw = 'Here is the response you asked for:\n{"title":"Prose","tabs":[]}\nHope that helps.'
    assert extract_json(raw)["title"] == "Prose"


def test_trailing_commas_are_repaired():
    raw = '{"title":"Trailing","metrics":[{"value":"1","label":"x",},],"tabs":[]}'
    parsed = extract_json(raw)
    assert parsed is not None
    assert parsed["title"] == "Trailing"


def test_unparseable_returns_none():
    assert extract_json("I could not complete that request.") is None
    assert extract_json("") is None
    assert extract_json("[1,2,3]") is None  # a list is not a document
