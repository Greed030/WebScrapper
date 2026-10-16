"""Claude через официальный SDK anthropic. Схема агента передаётся в structured outputs
(output_config.format), поэтому ответ приходит валидным JSON."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from ..config import LLMConfig
from ..errors import ScraperError
from .base import LLMProvider, api_key_from_env

log = logging.getLogger(__name__)

# Ограничения, которые structured outputs не принимает; исходная схема всё равно проверяется у нас.
_UNSUPPORTED_KEYWORDS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "patternProperties", "propertyNames",
    "dependentRequired", "dependentSchemas", "if", "then", "else", "not",
    "contains", "minContains", "maxContains", "unevaluatedProperties", "unevaluatedItems",
}
_SUPPORTED_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
_LITERAL_KEYWORDS = {"enum", "const", "default", "examples", "required", "description", "title"}


def to_structured_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Схема для output_config.format: additionalProperties=false у всех объектов, без
    неподдерживаемых ограничений. None — если схема с $ref (рекурсию API не принимает)."""
    if '"$ref"' in json.dumps(schema):
        return None

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _UNSUPPORTED_KEYWORDS or (key == "format" and value not in _SUPPORTED_FORMATS):
                continue
            if key == "additionalProperties":
                continue
            if key in _LITERAL_KEYWORDS:
                out[key] = value
            elif key == "properties" and isinstance(value, dict):
                out[key] = {name: walk(sub) for name, sub in value.items()}
            else:
                out[key] = walk(value)
        node_type = out.get("type")
        if node_type == "object" or (isinstance(node_type, list) and "object" in node_type) or "properties" in out:
            out["additionalProperties"] = False
        return out

    return walk(schema)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        kwargs: dict[str, Any] = {"timeout": cfg.timeout, "max_retries": 2}
        # ANTHROPIC_API_KEY (и профиль `ant auth login`) SDK находит сам; явно передаём только ключ
        # из нестандартной переменной.
        if cfg.api_key_env and cfg.api_key_env != "ANTHROPIC_API_KEY" and (key := api_key_from_env(cfg)):
            kwargs["api_key"] = key
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> str:
        output_config: dict[str, Any] = {}
        if self.cfg.effort:
            output_config["effort"] = self.cfg.effort
        structured = to_structured_schema(schema)
        if structured is not None:
            output_config["format"] = {"type": "json_schema", "schema": structured}

        try:
            response = await self._create(system, user, output_config)
        except anthropic.BadRequestError as e:
            if "format" not in output_config:
                raise _llm_error(e) from e
            log.warning("API не принял схему для structured outputs (%s), повтор без неё", e.message)
            output_config.pop("format")
            try:
                response = await self._create(system, user, output_config)
            except anthropic.APIError as retry_error:
                raise _llm_error(retry_error) from retry_error
        except anthropic.APIError as e:
            raise _llm_error(e) from e

        if response.stop_reason == "refusal":
            raise ScraperError("llm_error", "the model declined to process this page")
        if response.stop_reason == "max_tokens":
            raise ScraperError("llm_error", f"the answer hit max_tokens ({self.cfg.max_tokens}); raise llm.max_tokens")
        return "".join(block.text for block in response.content if block.type == "text")

    async def _create(self, system: str, user: str, output_config: dict[str, Any]) -> anthropic.types.Message:
        params: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if output_config:
            params["output_config"] = output_config
        try:
            return await self._client.messages.create(**params)
        except TypeError as e:  # SDK не нашёл ни ключа, ни профиля `ant auth login`
            if "authentication" not in str(e).lower():
                raise
            raise ScraperError(
                "llm_error", "no Anthropic credentials: set ANTHROPIC_API_KEY (see llm.api_key_env) "
                "or run `ant auth login`"
            ) from e

    async def aclose(self) -> None:
        await self._client.close()


def _llm_error(error: anthropic.APIError) -> ScraperError:
    if isinstance(error, anthropic.AuthenticationError):
        return ScraperError("llm_error", "Anthropic API key is missing or invalid (see llm.api_key_env)")
    if isinstance(error, anthropic.RateLimitError):
        return ScraperError("llm_error", "Anthropic API rate limit exceeded", retryable=True)
    if isinstance(error, anthropic.APIStatusError):
        return ScraperError("llm_error", f"Anthropic API error {error.status_code}: {error.message}",
                            retryable=error.status_code >= 500)
    if isinstance(error, anthropic.APIConnectionError):
        return ScraperError("llm_error", f"cannot reach the Anthropic API: {error}", retryable=True)
    return ScraperError("llm_error", f"Anthropic API error: {error}")
