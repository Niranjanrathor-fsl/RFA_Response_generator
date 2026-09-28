"""Wraps our existing Azure OpenAI chat deployment as a DeepEval judge model.

DeepEval's metrics (Faithfulness, AnswerRelevancy, ...) need an LLM to grade
the answer. Rather than depend on deepeval's own OpenAI/Azure integration
(API surface changes between versions), we point it at the same gpt-5.4
deployment already used for generation, via a small adapter.
"""

from __future__ import annotations

from deepeval.models import DeepEvalBaseLLM

from app.config import Settings, get_settings
from app.llm import LLMClient


class AzureJudgeModel(DeepEvalBaseLLM):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = LLMClient(self.settings)

    def load_model(self):
        return self._client

    def generate(self, prompt: str) -> str:
        return self._client.ask("You are a precise, impartial evaluator.", prompt)

    async def a_generate(self, prompt: str) -> str:
        # DeepEval can call either the sync or async path; the underlying SDK
        # call is synchronous, so just delegate.
        return self.generate(prompt)

    def get_model_name(self) -> str:
        return f"azure-openai:{self.settings.azure_openai_deployment_name}"
