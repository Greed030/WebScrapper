"""Локальная модель через Ollama /api/chat; JSON-схема передаётся в параметр format."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import LLMConfig
from ..errors import ScraperError
from .base import LLMProvider

DEFAULT_OLLAMA_URL = "http://localhost:11434"


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or DEFAULT_OLLAMA_URL).rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=cfg.timeout)

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> str:
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            response = await self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as e:
            raise ScraperError("llm_error", f"cannot reach Ollama at {self.base_url}: {e}", retryable=True) from e
        if response.status_code == 404:
            model = self.cfg.model
            raise ScraperError("llm_error", f"Ollama has no model '{model}': run `ollama pull {model}`")
        if response.status_code >= 400:
            raise ScraperError("llm_error", f"Ollama returned HTTP {response.status_code}: {response.text[:300]}",
                               retryable=response.status_code >= 500)
        return (response.json().get("message") or {}).get("content", "")

    async def aclose(self) -> None:
        await self._client.aclose()
