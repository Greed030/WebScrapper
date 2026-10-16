"""fetch: плагин → кэш → httpx → при необходимости Playwright → разбор → обрезка."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..config import Config
from ..core.browser import BrowserRenderer
from ..core.cache import PageCache
from ..core.detect import detect_block, needs_js
from ..core.http import HttpClient, raise_for_status
from ..core.parse import ParsedPage, content_kind, decode_body, parse_page
from ..core.urls import normalize_url
from ..errors import ScraperError
from ..models import ErrorInfo, FetchRequest, FetchResult, PageSummary, RawPage, RenderMode

if TYPE_CHECKING:
    from ..plugins import PluginRegistry

log = logging.getLogger(__name__)

def slice_text(text: str, offset: int, max_chars: int) -> tuple[str, int | None]:
    """Кусок текста [offset, offset+max_chars), обрезанный по границе абзаца/строки/слова.

    Возвращает (кусок, next_offset или None, если текст закончился).
    """
    if offset >= len(text):
        return "", None
    end = offset + max_chars
    if end >= len(text):
        return text[offset:], None
    floor = offset + int(max_chars * 0.8)
    for separator in ("\n\n", "\n", ". ", " "):
        position = text.rfind(separator, floor, end)
        if position != -1:
            end = position + len(separator)
            break
    return text[offset:end].rstrip(), end


class TextBudget:
    """Бюджет символов для пакетных операций: лимит на страницу и на весь ответ."""

    def __init__(self, per_page: int, total: int) -> None:
        self.per_page = per_page
        self.remaining = total

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def take(self, summary: PageSummary) -> None:
        if summary.error is not None:
            return
        full = summary.text
        limit = min(self.per_page, self.remaining)
        chunk = slice_text(full, 0, limit)[0] if limit > 0 else ""
        summary.text = chunk
        summary.total_chars = len(full)
        summary.truncated = len(chunk) < len(full)
        self.remaining -= len(chunk)


class Fetcher:
    def __init__(
        self,
        config: Config,
        http: HttpClient,
        cache: PageCache,
        browser: BrowserRenderer | None,
        plugins: PluginRegistry | None = None,
    ) -> None:
        self.config = config
        self.http = http
        self.cache = cache
        self.browser = browser
        self.plugins = plugins

    # ------------------------------------------------------------------ загрузка

    async def get_page(self, url: str, *, render: RenderMode = "auto", use_cache: bool = True) -> RawPage:
        plugin = self.plugins.match(url) if self.plugins else None
        if plugin is not None:
            page = await plugin.fetch(url, render=render, use_cache=use_cache)
            if page is not None:
                return page

        key = normalize_url(url)
        if use_cache:
            cached = await self.cache.get(key)
            if cached is not None and not (render == "always" and cached.via != "playwright"):
                log.debug("кэш: %s", url)
                return cached.model_copy(update={"url": url})

        page = await self._download(url, render)
        await self.cache.put(key, page)
        return page

    async def _download(self, url: str, render: RenderMode) -> RawPage:
        if render == "always":
            return await self._render(url)

        response = await self.http.get(url)
        kind = content_kind(response.content_type, response.content)
        body = decode_body(response.content, response.content_type) if kind else ""

        reason = detect_block(body, response.status_code) if kind == "html" else None
        if reason:
            if render == "auto" and self.browser is not None:
                log.info("%s: %s, пробуем браузер", url, reason)
                return await self._render(url)
            raise ScraperError("blocked", reason, url=url, status_code=response.status_code)

        raise_for_status(response.status_code, url)
        if kind is None:
            raise ScraperError(
                "unsupported_content",
                f"content type {response.content_type or 'unknown'} is not HTML or text",
                url=url,
                status_code=response.status_code,
            )

        page = RawPage(
            url=url,
            final_url=response.url,
            status_code=response.status_code,
            content_type=response.content_type,
            body=body,
            is_html=kind == "html",
            via="httpx",
            fetched_at=time.time(),
        )
        if (
            page.is_html
            and render == "auto"
            and self.browser is not None
            and await asyncio.to_thread(needs_js, body, self.config.browser.min_text_chars)
        ):
            log.info("%s: похоже на JS-страницу, рендерим в браузере", url)
            try:
                return await self._render(url)
            except ScraperError as e:
                log.warning("%s: браузер не помог (%s), отдаём HTML из httpx", url, e)
        return page

    async def _render(self, url: str) -> RawPage:
        if self.browser is None:
            raise ScraperError("invalid_input", "browser rendering is disabled on the server (browser.enabled)",
                               url=url)
        rendered = await self.browser.render(url, self.http.user_agent(url))
        reason = detect_block(rendered.html, rendered.status_code)
        if reason:
            raise ScraperError("blocked", reason, url=url, status_code=rendered.status_code)
        raise_for_status(rendered.status_code, url)
        kind = content_kind(rendered.content_type, rendered.html[:1024].encode("utf-8", "ignore"))
        if kind is None:
            raise ScraperError("unsupported_content", f"content type {rendered.content_type} is not HTML or text",
                               url=url)
        return RawPage(
            url=url,
            final_url=rendered.url,
            status_code=rendered.status_code,
            content_type=rendered.content_type,
            body=rendered.html,
            via="playwright",
            fetched_at=time.time(),
        )

    # ------------------------------------------------------------------ результаты

    async def parse(self, page: RawPage) -> ParsedPage:
        return await asyncio.to_thread(parse_page, page, phone_region=self.config.contacts.phone_region)

    async def fetch(self, req: FetchRequest) -> FetchResult:
        page = await self.get_page(req.url, render=req.render, use_cache=req.use_cache)
        parsed = await self.parse(page)
        out = self.config.output
        max_chars = min(req.max_chars or out.max_chars, out.max_chars_limit)
        chunk, next_offset = slice_text(parsed.text, req.offset, max_chars)
        return FetchResult(
            url=req.url,
            final_url=page.final_url,
            status_code=page.status_code,
            via=page.via,
            from_cache=page.from_cache,
            content_type=page.content_type,
            title=parsed.title,
            meta=parsed.meta,
            text=chunk,
            offset=req.offset,
            total_chars=len(parsed.text),
            truncated=next_offset is not None,
            next_offset=next_offset,
            links=parsed.links[: out.max_links] if req.offset == 0 else [],
            links_total=len(parsed.links),
            emails=parsed.emails,
            phones=parsed.phones,
        )

    async def summarize(self, url: str, *, render: RenderMode = "auto") -> tuple[PageSummary, ParsedPage | None]:
        """Страница для crawl/search_and_fetch. Текст полный — обрезает TextBudget.
        Ошибка не пробрасывается, а кладётся в summary.error."""
        try:
            page = await self.get_page(url, render=render)
            parsed = await self.parse(page)
        except ScraperError as e:
            return PageSummary(url=url, error=e.to_info()), None
        except Exception as e:  # одна сломанная страница не должна ронять весь обход
            log.exception("ошибка при обработке %s", url)
            return PageSummary(url=url, error=ErrorInfo(kind="internal", message=f"{type(e).__name__}: {e}",
                                                       url=url)), None
        summary = PageSummary(
            url=url,
            final_url=page.final_url,
            status_code=page.status_code,
            via=page.via,
            title=parsed.title,
            text=parsed.text,
            total_chars=len(parsed.text),
            emails=parsed.emails,
            phones=parsed.phones,
        )
        return summary, parsed
