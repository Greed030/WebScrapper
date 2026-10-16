"""Ограничение частоты запросов на домен (в пределах одного процесса)."""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict

from .urls import domain_matches, host_key


class DomainRateLimiter:
    """Выдерживает минимальный интервал между запросами к одному домену.

    Интервал = 1 / rps. Переопределения из конфига матчатся по суффиксу:
    «linkedin.com» покрывает www.linkedin.com и любые поддомены.
    """

    def __init__(self, default_rps: float, overrides: dict[str, float] | None = None) -> None:
        self.default_rps = default_rps
        self.overrides = {host_key(d): rps for d, rps in (overrides or {}).items()}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = {}

    def _resolve(self, url_or_host: str) -> tuple[str, float]:
        host = host_key(url_or_host)
        for domain, rps in self.overrides.items():
            if domain_matches(host, domain):
                return domain, rps
        return host, self.default_rps

    def interval_for(self, url_or_host: str) -> float:
        _, rps = self._resolve(url_or_host)
        return 1.0 / rps if rps > 0 else 0.0

    async def acquire(self, url_or_host: str, *, min_interval: float = 0.0, jitter: float = 0.0) -> None:
        key, rps = self._resolve(url_or_host)
        interval = max(1.0 / rps if rps > 0 else 0.0, min_interval)
        if interval <= 0:
            return
        # Лок держится во время ожидания — так запросы к домену идут строго по очереди.
        async with self._locks[key]:
            last = self._last.get(key)
            if last is not None:
                wait = last + interval + random.uniform(0, jitter) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last[key] = time.monotonic()
