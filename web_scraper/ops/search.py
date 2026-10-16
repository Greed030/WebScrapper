"""search: веб-поиск через JSON API self-hosted SearXNG."""

from __future__ import annotations

import logging
import re

from ..config import SearxngConfig
from ..core.http import HttpClient
from ..core.urls import normalize_url
from ..errors import ScraperError
from ..models import SearchHit, SearchRequest, SearchResult

log = logging.getLogger(__name__)


def searxng_language(lang: str | None, region: str | None) -> str | None:
    """«de» + «DE» → «de-DE»; SearXNG принимает язык и язык-регион одним параметром."""
    if region:
        return f"{(lang or 'en').lower()}-{region.upper()}"
    return lang.lower() if lang else None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


async def search(req: SearchRequest, cfg: SearxngConfig, http: HttpClient) -> SearchResult:
    endpoint = f"{cfg.url.rstrip('/')}/search"
    language = searxng_language(req.lang or cfg.default_lang, req.region or cfg.default_region)
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for pageno in range(1, cfg.max_pages + 1):
        params: dict[str, str | int] = {"q": req.query, "format": "json", "pageno": pageno}
        if language:
            params["language"] = language
        try:
            response = await http.get(
                endpoint,
                params=params,
                headers={"Accept": "application/json"},
                rate_limit=False,
                retries=1,
                timeout=cfg.timeout,
            )
        except ScraperError as e:
            if e.kind in ("network", "not_found"):
                raise ScraperError("network", f"SearXNG is unreachable at {cfg.url}: {e.message}",
                                   url=cfg.url, retryable=True) from e
            raise

        if response.status_code == 403:
            raise ScraperError("http_error", "SearXNG returned 403: enable the json format "
                               "(search.formats: [html, json]) in its settings.yml", url=cfg.url, status_code=403)
        if response.status_code == 429:
            raise ScraperError("blocked", "SearXNG limiter rejected the request: disable server.limiter "
                               "or allow this client in limiter.toml", url=cfg.url, status_code=429, retryable=True)
        if response.status_code >= 400:
            raise ScraperError("http_error", f"SearXNG returned HTTP {response.status_code}", url=cfg.url,
                               status_code=response.status_code, retryable=response.status_code >= 500)
        try:
            payload = response.json()
        except ValueError as e:
            raise ScraperError("http_error", "SearXNG returned a non-JSON response", url=cfg.url) from e

        if payload.get("unresponsive_engines"):
            log.info("SearXNG: не ответили движки %s", payload["unresponsive_engines"])

        added = 0
        for item in payload.get("results") or []:
            url = item.get("url")
            if not url or not url.startswith(("http://", "https://")):
                continue
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                SearchHit(
                    title=_clean(item.get("title")) or url,
                    url=url,
                    snippet=_clean(item.get("content")),
                    engine=item.get("engine"),
                )
            )
            added += 1
            if len(hits) >= req.num_results:
                break
        if len(hits) >= req.num_results or added == 0:
            break

    return SearchResult(query=req.query, results=hits[: req.num_results])
