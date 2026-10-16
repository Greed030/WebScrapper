"""crawl: обход сайта в ширину с лимитами глубины/страниц, фильтром по регулярке и robots.txt."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

from ..config import Config
from ..core.parse import ParsedPage
from ..core.robots import RobotsChecker
from ..core.urls import host_key, normalize_url
from ..models import CrawledPage, CrawlRequest, CrawlResult
from .fetch import Fetcher, TextBudget

log = logging.getLogger(__name__)

# Ссылки на файлы, которые точно не HTML, — не тратим на них лимит страниц.
_SKIP_EXTENSIONS = re.compile(
    r"\.(?:jpe?g|png|gif|webp|avif|svg|ico|bmp|tiff?|pdf|zip|rar|7z|gz|tgz|tar|bz2|mp[34]|m4a|avi|mov|mkv|webm|"
    r"wav|ogg|exe|msi|dmg|apk|iso|docx?|xlsx?|pptx?|odt|css|js|json|xml|woff2?|ttf|eot)$",
    re.I,
)


async def crawl(req: CrawlRequest, *, config: Config, fetcher: Fetcher, robots: RobotsChecker | None) -> CrawlResult:
    caps = config.crawl
    warnings: list[str] = []
    max_depth = min(req.max_depth, caps.max_depth_cap)
    max_pages = min(req.max_pages, caps.max_pages_cap)
    if max_depth < req.max_depth:
        warnings.append(f"max_depth limited to {max_depth} by server config")
    if max_pages < req.max_pages:
        warnings.append(f"max_pages limited to {max_pages} by server config")

    pattern = re.compile(req.url_pattern) if req.url_pattern else None
    allowed_hosts = {host_key(req.start_url)}
    budget = TextBudget(req.max_chars_per_page or config.output.page_chars_in_batch, config.output.max_total_chars)
    semaphore = asyncio.Semaphore(caps.concurrency)

    async def visit(url: str, depth: int) -> tuple[CrawledPage, ParsedPage | None] | None:
        async with semaphore:
            if robots is not None and not await robots.allowed(url):
                log.info("robots.txt запрещает %s", url)
                return None
            summary, parsed = await fetcher.summarize(url, render=req.render)
            return CrawledPage(**summary.model_dump(), depth=depth), parsed

    visited = {normalize_url(req.start_url)}
    frontier = [req.start_url]
    pages: list[CrawledPage] = []
    skipped_by_robots = 0
    stop_reason = "completed"

    for depth in range(max_depth + 1):
        if not frontier:
            break
        room = max_pages - len(pages)
        batch, overflow = frontier[:room], len(frontier) > room
        results = await asyncio.gather(*(visit(url, depth) for url in batch))

        next_frontier: list[str] = []
        for result in results:
            if result is None:
                skipped_by_robots += 1
                continue
            page, parsed = result
            budget.take(page)
            pages.append(page)
            if page.final_url:
                visited.add(normalize_url(page.final_url))
                if depth == 0:
                    allowed_hosts.add(host_key(page.final_url))  # стартовый URL мог редиректнуть на другой хост
            if parsed is None or depth == max_depth:
                continue
            for link in parsed.links:
                candidate = link.url
                if req.same_domain and host_key(candidate) not in allowed_hosts:
                    continue
                if _SKIP_EXTENSIONS.search(urlsplit(candidate).path):
                    continue
                if pattern is not None and not pattern.search(candidate):
                    continue
                key = normalize_url(candidate)
                if key not in visited:
                    visited.add(key)
                    next_frontier.append(candidate)

        if budget.exhausted:
            stop_reason = "char_budget"
            break
        if len(pages) >= max_pages:
            if overflow or next_frontier:
                stop_reason = "max_pages"
            break
        frontier = next_frontier

    failed = sum(1 for p in pages if p.error is not None)
    return CrawlResult(
        start_url=req.start_url,
        pages=pages,
        pages_ok=len(pages) - failed,
        pages_failed=failed,
        skipped_by_robots=skipped_by_robots,
        stop_reason=stop_reason,
        warnings=warnings,
    )
