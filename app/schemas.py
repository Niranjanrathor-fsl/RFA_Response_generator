"""Pydantic models for the structured response the model returns.

Validating the model output here means the generators (HTML/docx/pptx/xlsx) can
rely on a stable shape instead of defensively poking at raw dictionaries.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ICON_KEYWORDS = {
    "doc", "chart", "target", "check", "shield", "gear", "bulb",
    "layers", "users", "clock", "growth", "lock", "flag",
}

OutputFormat = Literal["dashboard", "qa", "docx", "pptx", "xlsx"]
Mode = Literal["rfi", "summary"]
QualityFlag = Literal["green", "amber", "red"]


class Metric(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str = ""
    label: str = ""
    accent: bool = False
    icon: Optional[str] = None

    @field_validator("icon")
    @classmethod
    def _known_icon(cls, v: Optional[str]) -> Optional[str]:
        if v and v.lower() in ICON_KEYWORDS:
            return v.lower()
        return None

    @field_validator("value", "label", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str:
        return "" if v is None else str(v)


class QAItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    n: str = ""
    q: str = ""
    a: str = ""

    @field_validator("n", "q", "a", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str:
        return "" if v is None else str(v)


class Table(BaseModel):
    model_config = ConfigDict(extra="ignore")

    headers: List[str] = Field(default_factory=list)
    rows: List[List[str]] = Field(default_factory=list)

    @field_validator("headers", mode="before")
    @classmethod
    def _string_headers(cls, v: object) -> List[str]:
        if not isinstance(v, list):
            return []
        return ["" if c is None else str(c) for c in v]

    @field_validator("rows", mode="before")
    @classmethod
    def _string_rows(cls, v: object) -> List[List[str]]:
        if not isinstance(v, list):
            return []
        out: List[List[str]] = []
        for row in v:
            if isinstance(row, list):
                out.append(["" if c is None else str(c) for c in row])
            elif row is not None:
                out.append([str(row)])
        return out


class Callout(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    body: str = ""

    @field_validator("title", "body", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str:
        return "" if v is None else str(v)


class Tab(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = "Overview"
    intro: str = ""
    bullets: List[str] = Field(default_factory=list)
    qa: List[QAItem] = Field(default_factory=list)
    table: Optional[Table] = None
    callout: Optional[Callout] = None
    icon: Optional[str] = None

    @field_validator("bullets", mode="before")
    @classmethod
    def _string_bullets(cls, v: object) -> List[str]:
        if not isinstance(v, list):
            return []
        return [str(b) for b in v if b is not None and str(b).strip()]

    @field_validator("icon")
    @classmethod
    def _known_icon(cls, v: Optional[str]) -> Optional[str]:
        if v and v.lower() in ICON_KEYWORDS:
            return v.lower()
        return None

    @field_validator("name", "intro", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str:
        return "" if v is None else str(v)


class SourceInfo(BaseModel):
    name: str
    chars: int = 0
    words: int = 0
    note: str = ""


class ResponseDocument(BaseModel):
    """The validated payload that every output format is rendered from."""

    model_config = ConfigDict(extra="ignore")

    title: str = "Firstsource Response"
    subtitle: str = ""
    metrics: List[Metric] = Field(default_factory=list)
    tabs: List[Tab] = Field(default_factory=list)

    @field_validator("title", "subtitle", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str:
        return "" if v is None else str(v)

    @property
    def question_count(self) -> int:
        return sum(len(tab.qa) for tab in self.tabs)

    @property
    def mode(self) -> Mode:
        return "rfi" if self.question_count else "summary"


class QualityAssessment(BaseModel):
    """Live per-response groundedness self-check (separate from the offline
    DeepEval harness in tests/eval/) - a single fast LLM call so the UI can show
    an immediate green/amber/red signal right after generation."""

    model_config = ConfigDict(extra="ignore")

    score: int = Field(ge=0, le=100)
    flag: QualityFlag
    reason: str = ""


class GenerateResult(BaseModel):
    """What POST /api/generate hands back to the browser."""

    document: ResponseDocument
    mode: Mode
    question_count: int
    sources: List[SourceInfo]
    model: str
    corpus_chars: int
    truncated: bool = False
    quality: Optional[QualityAssessment] = None


class RenderRequest(BaseModel):
    """Body for POST /api/render/{fmt} - lets the browser re-download without
    paying for another model call."""

    document: ResponseDocument
    sources: List[SourceInfo] = Field(default_factory=list)
