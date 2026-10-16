"""OpenAI (или совместимый API через base_url) в JSON mode."""

from __future__ import annotations

from typing import Any

import openai

from ..config import LLMConfig
from ..errors import ScraperError
from .base import LLMProvider, api_key_from_env


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._client = openai.AsyncOpenAI(
            api_key=api_key_from_env(cfg),  # None → SDK возьмёт OPENAI_API_KEY
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            max_retries=2,
        )

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self.cfg.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
            )
        except openai.AuthenticationError as e:
            raise ScraperError("llm_error", "OpenAI API key is missing or invalid (see llm.api_key_env)") from e
        except openai.RateLimitError as e:
            raise ScraperError("llm_error", "OpenAI API rate limit exceeded", retryable=True) from e
        except openai.APIStatusError as e:
            raise ScraperError("llm_error", f"OpenAI API error {e.status_code}: {e.message}",
                               retryable=e.status_code >= 500) from e
        except openai.APIConnectionError as e:
            raise ScraperError("llm_error", f"cannot reach the OpenAI API: {e}", retryable=True) from e

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise ScraperError("llm_error", "the answer was cut off by the model's output limit")
        return choice.message.content or ""

    async def aclose(self) -> None:
        await self._client.close()
