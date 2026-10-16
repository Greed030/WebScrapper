"""CLI для ручной проверки: python -m web_scraper fetch https://example.com"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .log import setup_logging
from .models import Envelope
from .scraper import Scraper


def _json_arg(value: str) -> Any:
    """JSON прямо в аргументе или @путь/к/файлу.json (в PowerShell так проще с кавычками)."""
    try:
        if value.startswith("@"):
            return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
        return json.loads(value)
    except (OSError, json.JSONDecodeError) as e:
        raise argparse.ArgumentTypeError(f"не удалось прочитать JSON: {e}") from e


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web_scraper", description="Универсальный скрапер для AI-агента")
    parser.add_argument("--config", help="путь к config.yaml (по умолчанию $WEB_SCRAPER_CONFIG или ./config.yaml)")
    parser.add_argument("--log-level", help="DEBUG / INFO / WARNING (перекрывает конфиг)")
    sub = parser.add_subparsers(dest="command", required=True)
    render = {"choices": ["auto", "always", "never"], "help": "когда использовать браузер"}

    p = sub.add_parser("search", help="веб-поиск через SearXNG")
    p.add_argument("query")
    p.add_argument("-n", "--num-results", type=int)
    p.add_argument("--lang")
    p.add_argument("--region")

    p = sub.add_parser("fetch", help="загрузить страницу")
    p.add_argument("url")
    p.add_argument("--render", **render)
    p.add_argument("--max-chars", type=int)
    p.add_argument("--offset", type=int)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--text", action="store_true", help="напечатать только текст страницы")

    p = sub.add_parser("extract", help="структурированное извлечение")
    p.add_argument("url")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--selectors", type=_json_arg, help="JSON селекторов или @file.json")
    group.add_argument("--schema", type=_json_arg, help="JSON Schema для LLM-режима или @file.json")
    p.add_argument("--instructions")
    p.add_argument("--render", **render)
    p.add_argument("--no-cache", action="store_true")

    p = sub.add_parser("crawl", help="обход сайта")
    p.add_argument("url")
    p.add_argument("--depth", type=int)
    p.add_argument("--pages", type=int)
    p.add_argument("--pattern", help="регулярка для ссылок")
    p.add_argument("--any-domain", action="store_true", help="ходить и по внешним ссылкам")
    p.add_argument("--render", **render)
    p.add_argument("--chars-per-page", type=int)

    p = sub.add_parser("search-and-fetch", help="поиск + загрузка топ-N")
    p.add_argument("query")
    p.add_argument("-n", type=int)
    p.add_argument("--lang")
    p.add_argument("--region")
    p.add_argument("--render", **render)
    p.add_argument("--chars-per-page", type=int)

    p = sub.add_parser("serve", help="запустить HTTP API")
    p.add_argument("--host")
    p.add_argument("--port", type=int)

    sub.add_parser("tools", help="напечатать tool definitions (JSON)")
    sub.add_parser("mcp", help="запустить MCP-сервер на stdio")
    return parser


def _payload(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Имя операции и запрос; незаданные опции не передаются, чтобы работали значения по умолчанию."""
    if args.command == "search":
        op, data = "search", {"query": args.query, "num_results": args.num_results, "lang": args.lang,
                              "region": args.region}
    elif args.command == "fetch":
        op, data = "fetch", {"url": args.url, "render": args.render, "max_chars": args.max_chars,
                             "offset": args.offset, "use_cache": False if args.no_cache else None}
    elif args.command == "extract":
        op = "extract"
        data = {"url": args.url, "render": args.render, "use_cache": False if args.no_cache else None,
                "instructions": args.instructions}
        if args.selectors is not None:
            data.update(mode="selectors", selectors=args.selectors)
        else:
            data.update(mode="llm", schema=args.schema)
    elif args.command == "crawl":
        op, data = "crawl", {"start_url": args.url, "max_depth": args.depth, "max_pages": args.pages,
                             "url_pattern": args.pattern, "same_domain": False if args.any_domain else None,
                             "render": args.render, "max_chars_per_page": args.chars_per_page}
    else:
        op, data = "search_and_fetch", {"query": args.query, "n": args.n, "lang": args.lang, "region": args.region,
                                        "render": args.render, "max_chars_per_page": args.chars_per_page}
    return op, {key: value for key, value in data.items() if value is not None}


async def _execute(config: Config, op: str, payload: dict[str, Any]) -> Envelope:
    async with Scraper(config) as scraper:
        return await scraper.call(op, payload)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # иначе консоль Windows падает на кириллице и эмодзи
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    setup_logging(config.logging, args.log_level)

    if args.command == "tools":
        from .tools import tool_definitions

        print(json.dumps(tool_definitions(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(config), host=args.host or config.server.host, port=args.port or config.server.port)
        return 0
    if args.command == "mcp":
        from .mcp_server import run_stdio

        asyncio.run(run_stdio(config))
        return 0

    op, payload = _payload(args)
    envelope = asyncio.run(_execute(config, op, payload))
    if op == "fetch" and args.text and envelope.ok:
        print(envelope.result.text)
    else:
        print(envelope.model_dump_json(indent=2, exclude_none=True))
    return 0 if envelope.ok else 1
