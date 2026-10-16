"""Проверка robots.txt для crawl (RFC 9309: 4xx — можно всё, 5xx — нельзя ничего)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from ..errors import ScraperError
from .http import HttpClient
from .parse import decode_body

log = logging.getLogger(__name__)


class RobotsChecker:
    def __init__(self, http: HttpClient, ttl_seconds: float = 3600.0) -> None:
        self.http = http
        self.ttl = ttl_seconds
        self._parsers: dict[str, tuple[float, RobotFileParser]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        parser = await self._parser(f"{parts.scheme}://{parts.netloc}")
        return parser.can_fetch("*", url)

    async def _parser(self, origin: str) -> RobotFileParser:
        async with self._locks[origin]:
            cached = self._parsers.get(origin)
            if cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1]
            robots_url = f"{origin}/robots.txt"
            parser = RobotFileParser(robots_url)
            try:
                response = await self.http.get(robots_url, retries=1, headers={"Accept": "text/plain,*/*;q=0.8"})
            except ScraperError as e:
                log.info("robots.txt недоступен (%s), обход разрешён: %s", robots_url, e)
                parser.allow_all = True
            else:
                if 200 <= response.status_code < 300:
                    parser.parse(decode_body(response.content, response.content_type).splitlines())
                elif response.status_code >= 500:
                    log.info("robots.txt вернул %s — обход %s запрещён", response.status_code, origin)
                    parser.disallow_all = True
                else:
                    parser.allow_all = True
            self._parsers[origin] = (time.monotonic(), parser)
            return parser
