"""Интерфейс LLM-провайдера и фабрика по конфигу."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from ..config import LLMConfig
from ..errors import ScraperError


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> str:
        """Вернуть сырой текст ответа модели (ожидается JSON). Валидацию делает вызывающий код."""

    async def aclose(self) -> None:
        return None


def api_key_from_env(cfg: LLMConfig) -> str | None:
    return os.environ.get(cfg.api_key_env) if cfg.api_key_env else None


def create_provider(cfg: LLMConfig) -> LLMProvider:
    """SDK импортируются лениво: без ключей и extras сервер стартует, падает только LLM-режим."""
    try:
        if cfg.provider == "anthropic":
            from .anthropic_provider import AnthropicProvider

            return AnthropicProvider(cfg)
        if cfg.provider == "openai":
            from .openai_provider import OpenAIProvider

            return OpenAIProvider(cfg)
        from .ollama_provider import OllamaProvider

        return OllamaProvider(cfg)
    except ImportError as e:
        raise ScraperError(
            "llm_error",
            f"LLM provider '{cfg.provider}' needs an extra package: pip install 'web-scraper[{cfg.provider}]'",
        ) from e
    except ScraperError:
        raise
    except Exception as e:  # SDK падает в конструкторе, если не нашёл ключ
        raise ScraperError("llm_error", f"cannot initialise LLM provider '{cfg.provider}': {e}") from e
