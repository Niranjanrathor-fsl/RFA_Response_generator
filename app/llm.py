"""Azure OpenAI API client.

This module is the only place that talks to the Azure OpenAI API. The key and
endpoint come from environment variables and never leave the server.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from openai import AzureOpenAI, APIStatusError, APIConnectionError, OpenAIError

from .config import Settings, get_settings

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class LLMError(RuntimeError):
    """Raised when the model call fails or its output cannot be parsed."""


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first JSON object out of a model response.

    Handles code fences, leading prose and trailing commas - the three things that
    break a naive json.loads on real model output.
    """
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = cleaned[start : end + 1]
    for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
        try:
            parsed = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.azure_openai_api_key:
            raise LLMError(
                "AZURE_OPENAI_API_KEY is not configured on the server. "
                "Set it in the environment (see .env.example) and restart."
            )
        if not self.settings.azure_openai_endpoint:
            raise LLMError(
                "AZURE_OPENAI_ENDPOINT is not configured on the server. "
                "Set it in the environment (see .env.example) and restart."
            )
        self._client = AzureOpenAI(
            api_key=self.settings.azure_openai_api_key,
            azure_endpoint=self.settings.azure_openai_endpoint,
            api_version=self.settings.azure_openai_api_version,
            timeout=self.settings.azure_openai_timeout_seconds,
            max_retries=1,
        )

    @property
    def model(self) -> str:
        return self.settings.azure_openai_deployment_name

    def _call(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.settings.azure_openai_deployment_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except APIStatusError as exc:  # 4xx / 5xx from the API
            raise LLMError(
                f"Azure OpenAI API returned {exc.status_code}: {exc.message}"
            ) from exc
        except APIConnectionError as exc:
            raise LLMError(
                "Could not reach the Azure OpenAI API. Check the server's outbound "
                "network access and any proxy settings."
            ) from exc
        except OpenAIError as exc:
            raise LLMError(f"Azure OpenAI client error: {exc}") from exc

        return response.choices[0].message.content or ""

    def generate_document(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """Call the model and return the parsed JSON document structure."""
        raw = self._call(system_prompt, user_prompt)
        parsed = extract_json(raw)
        if parsed is None:
            log.warning("First response was not valid JSON; retrying with a nudge.")
            retry_prompt = (
                user_prompt
                + "\n\nYour previous reply could not be parsed as JSON. Reply with the "
                "JSON object ONLY - no prose, no markdown fences."
            )
            raw = self._call(system_prompt, retry_prompt)
            parsed = extract_json(raw)
        if parsed is None:
            raise LLMError(
                "The model did not return parseable JSON. Try again, or reduce the "
                "size of the source content."
            )
        if not parsed.get("tabs"):
            raise LLMError("The model response contained no tabs to render.")
        return parsed
