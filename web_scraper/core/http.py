"""HTTP-клиент поверх httpx: ретраи с backoff, Retry-After, ротация User-Agent, прокси, лимит размера."""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import HttpConfig
from ..errors import ScraperError
from .ratelimit import DomainRateLimiter
from .urls import domain_matches

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_BLOCKED_STATUSES = {401, 403, 407, 429, 451, 999}
_DNS_HINTS = ("getaddrinfo failed", "name or service not known", "nodename nor servname", "no address associated")


@dataclass
class HttpResponse:
    url: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    truncated: bool = False

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    def json(self) -> Any:
        return json.loads(self.content)


class HttpClient:
    def __init__(self, cfg: HttpConfig, limiter: DomainRateLimiter) -> None:
        self.cfg = cfg
        self.limiter = limiter
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=10,
            timeout=httpx.Timeout(cfg.timeout, connect=cfg.connect_timeout),
            proxy=cfg.proxy or None,
            verify=cfg.verify_ssl,
        )

    def user_agent(self, url: str | None = None) -> str:
        """User-Agent для запроса: переопределение для домена из конфига или случайный из списка."""
        if url:
            host = urlsplit(url).hostname or ""
            for domain, user_agent in self.cfg.user_agent_overrides.items():
                if domain_matches(host, domain):
                    return user_agent
        return random.choice(self.cfg.user_agents)

    def default_headers(self, user_agent: str | None = None) -> dict[str, str]:
        return {
            "User-Agent": user_agent or self.user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": self.cfg.accept_language,
        }

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        rate_limit: bool = True,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """GET с ретраями. Статус ответа не проверяет — классификацией занимается вызывающий код.

        Исключения httpx превращаются в ScraperError (timeout / network / not_found для DNS).
        """
        max_retries = self.cfg.max_retries if retries is None else retries
        request_headers = self.default_headers(self.user_agent(url))
        request_headers.update(headers or {})

        attempt = 0
        while True:
            if rate_limit:
                await self.limiter.acquire(url)
            try:
                response = await self._read(url, params, request_headers, timeout)
            except httpx.TimeoutException as e:
                error = ScraperError("timeout", f"request timed out ({type(e).__name__})", url=url)
            except httpx.ConnectError as e:
                if _is_dns_error(e):
                    raise ScraperError("not_found", "domain name could not be resolved", url=url) from e
                error = ScraperError("network", f"connection failed: {e}", url=url)
            except httpx.TooManyRedirects as e:
                raise ScraperError("http_error", "too many redirects", url=url) from e
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
                raise ScraperError("invalid_input", f"invalid URL: {e}", url=url) from e
            except httpx.HTTPError as e:
                error = ScraperError("network", f"{type(e).__name__}: {e}", url=url)
            else:
                if response.status_code in RETRY_STATUSES and attempt < max_retries:
                    delay = _retry_after(response.headers, self.cfg.backoff_max) or self._backoff(attempt)
                    log.warning("HTTP %s от %s, повтор %d/%d через %.1fс", response.status_code, url,
                                attempt + 1, max_retries, delay)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                return response

            if attempt >= max_retries:
                raise error
            delay = self._backoff(attempt)
            log.warning("%s для %s, повтор %d/%d через %.1fс", error, url, attempt + 1, max_retries, delay)
            await asyncio.sleep(delay)
            attempt += 1

    async def _read(
        self, url: str, params: dict[str, Any] | None, headers: dict[str, str], timeout: float | None
    ) -> HttpResponse:
        limit = self.cfg.max_response_bytes
        extra = {"timeout": timeout} if timeout is not None else {}
        async with self._client.stream("GET", url, params=params, headers=headers, **extra) as r:
            chunks: list[bytes] = []
            size = 0
            truncated = False
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= limit:
                    truncated = True
                    break
            content = b"".join(chunks)
            if truncated:
                log.warning("ответ %s обрезан до %d байт", url, limit)
                content = content[:limit]
            return HttpResponse(
                url=str(r.url),
                status_code=r.status_code,
                headers={k.lower(): v for k, v in r.headers.items()},
                content=content,
                truncated=truncated,
            )

    def _backoff(self, attempt: int) -> float:
        base = min(self.cfg.backoff_max, self.cfg.backoff_base * (2**attempt))
        return base * random.uniform(0.5, 1.5)

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(headers: dict[str, str], cap: float) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, cap))


def _is_dns_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, socket.gaierror) and current.errno != getattr(socket, "EAI_AGAIN", None):
            return True
        if any(hint in str(current).lower() for hint in _DNS_HINTS):
            return True
        current = current.__cause__ or current.__context__
    return False


def raise_for_status(status_code: int, url: str) -> None:
    """HTTP-статус ≥ 400 → ScraperError нужного типа (not_found / blocked / http_error)."""
    if status_code < 400:
        return
    if status_code in (404, 410):
        raise ScraperError("not_found", f"HTTP {status_code}", url=url, status_code=status_code)
    if status_code in _BLOCKED_STATUSES:
        raise ScraperError("blocked", f"HTTP {status_code}: access denied by the site", url=url,
                           status_code=status_code, retryable=status_code == 429)
    raise ScraperError("http_error", f"HTTP {status_code}", url=url, status_code=status_code,
                       retryable=status_code >= 500)
