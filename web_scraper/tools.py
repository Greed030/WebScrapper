"""Tool definitions в формате Claude API (name / description / input_schema) и клиент,
который исполняет вызовы инструментов через HTTP API скрапера.

    from web_scraper.tools import ScraperTools

    scraper = ScraperTools("http://127.0.0.1:8765")
    response = client.messages.create(..., tools=scraper.definitions)
    results = [await scraper.tool_result(b) for b in response.content if scraper.handles(b)]
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel

from .operations import OPERATIONS, operation_for_tool


def input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema запроса без служебного шума, который только тратит контекст агента:
    title убираются, `anyOf: [X, null]` у необязательных полей сворачивается в X."""
    schema = model.model_json_schema(by_alias=True)
    schema.pop("description", None)  # описание операции и так есть в description инструмента
    return _simplify(schema)


def _simplify(node: Any) -> Any:
    if isinstance(node, list):
        return [_simplify(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: _simplify(sub) for name, sub in value.items()}  # имена полей не трогаем
        else:
            out[key] = _simplify(value)
    variants = out.get("anyOf")
    if isinstance(variants, list) and len(variants) == 2 and {"type": "null"} in variants:
        out.pop("anyOf")
        out = {**next(v for v in variants if v != {"type": "null"}), **out}
        if out.get("default", ...) is None:
            out.pop("default")
    return out


def tool_definitions() -> list[dict[str, Any]]:
    """Список инструментов для параметра tools в client.messages.create()."""
    return [
        {"name": op.tool_name, "description": op.description, "input_schema": input_schema(op.request)}
        for op in OPERATIONS.values()
    ]


def _field(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _error_json(kind: str, message: str, retryable: bool = False) -> str:
    return json.dumps({"ok": False, "error": {"kind": kind, "message": message, "retryable": retryable}})


class ScraperTools:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        *,
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.definitions = tool_definitions()
        self._names = {definition["name"] for definition in self.definitions}
        self._client = client or httpx.AsyncClient(timeout=timeout)

    def handles(self, block_or_name: Any) -> bool:
        """True для tool_use-блока (объект SDK или dict) или имени одного из наших инструментов."""
        if isinstance(block_or_name, str):
            return block_or_name in self._names
        return _field(block_or_name, "type") == "tool_use" and _field(block_or_name, "name") in self._names

    async def run(self, name: str, tool_input: dict[str, Any] | None) -> str:
        """Выполнить инструмент и вернуть JSON-строку конверта {ok, result | error}."""
        operation = operation_for_tool(name)
        if operation is None:
            return _error_json("invalid_input", f"unknown tool {name!r}")
        try:
            response = await self._client.post(f"{self.base_url}/{operation.name}", json=tool_input or {})
        except httpx.HTTPError as e:
            return _error_json("network", f"web-scraper API is unreachable at {self.base_url}: {e}", retryable=True)
        return response.text

    async def tool_result(self, block: Any) -> dict[str, Any]:
        """tool_use-блок → готовый tool_result-блок для следующего сообщения пользователя."""
        content = await self.run(_field(block, "name"), _field(block, "input"))
        try:
            ok = bool(json.loads(content).get("ok"))
        except (ValueError, AttributeError):
            ok = False
        return {"type": "tool_result", "tool_use_id": _field(block, "id"), "content": content, "is_error": not ok}

    async def aclose(self) -> None:
        await self._client.aclose()
