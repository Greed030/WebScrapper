"""Базовый класс плагина для сайтов, которым не хватает общего пайплайна."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ..config import Config
from ..core.cache import PageCache
from ..core.http import HttpClient
from ..core.ratelimit import DomainRateLimiter
from ..core.urls import domain_matches
from ..models import ExtractRequest, ExtractResult, RawPage, RenderMode


@dataclass
class PluginContext:
    """Общие ресурсы скрапера, доступные плагину."""

    config: Config
    http: HttpClient
    cache: PageCache
    limiter: DomainRateLimiter


class BasePlugin:
    """Плагин перехватывает fetch и/или extract для своих доменов.

    - name: ключ в секции plugins конфига (там же enabled и прочие настройки плагина);
    - domains: домены, «example.com» покрывает и поддомены;
    - fetch: вернуть RawPage (HTML или готовый markdown) или None — тогда работает общий пайплайн;
    - extract: вернуть ExtractResult или None — тогда работают общие режимы selectors/llm
      по странице, которую вернул fetch плагина.
    """

    name: ClassVar[str] = ""
    domains: ClassVar[tuple[str, ...]] = ()
    enabled_by_default: ClassVar[bool] = False

    def __init__(self, ctx: PluginContext, settings: dict[str, Any]) -> None:
        self.ctx = ctx
        self.settings = settings

    def matches(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return any(domain_matches(host, domain) for domain in self.domains)

    async def fetch(self, url: str, *, render: RenderMode, use_cache: bool) -> RawPage | None:
        return None

    async def extract(self, req: ExtractRequest, page: RawPage) -> ExtractResult | None:
        return None

    async def aclose(self) -> None:
        return None
