"""LinkedIn: только публичные гостевые страницы и эндпоинты jobs-guest, без логина и cookies.

По умолчанию выключен. Запросы идут строго по одному с паузой (не меньше MIN_INTERVAL_FLOOR),
на 429/999 плагин уходит в «остывание» и не обращается к сайту, пока оно не закончится.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from ..core.http import raise_for_status
from ..core.parse import decode_body, html_to_text
from ..core.ratelimit import DomainRateLimiter
from ..core.urls import normalize_url
from ..errors import ScraperError
from ..models import RawPage, RenderMode
from .base import BasePlugin, PluginContext

log = logging.getLogger(__name__)

GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api"
MIN_INTERVAL_FLOOR = 3.0

_JOB_VIEW_RE = re.compile(r"^/jobs/view/(?:[^/]*?-)?(\d{6,})/?$")
_COMPANY_RE = re.compile(r"^/company/[^/]+/?$")
_SEARCH_PARAMS = ("keywords", "location", "geoId", "distance", "f_TPR", "f_WT", "f_E", "f_JT", "f_C", "f_I",
                  "sortBy", "start")
_AUTHWALL_PATHS = ("/authwall", "/login", "/signup", "/uas/login", "/checkpoint")
_UNSUPPORTED_MESSAGE = (
    "the LinkedIn plugin serves only public guest pages: /jobs/view/<id>, /jobs/search?keywords=...&location=..., "
    "/company/<name>; other LinkedIn pages require login"
)


def _text(node: LexborNode | None) -> str:
    return re.sub(r"\s+", " ", node.text()).strip() if node is not None else ""


class LinkedInPlugin(BasePlugin):
    name = "linkedin"
    domains = ("linkedin.com",)

    def __init__(self, ctx: PluginContext, settings: dict[str, Any]) -> None:
        super().__init__(ctx, settings)
        self.min_interval = max(float(settings.get("min_interval_s", 10.0)), MIN_INTERVAL_FLOOR)
        self.jitter = max(float(settings.get("jitter_s", 3.0)), 0.0)
        self.cooldown_s = float(settings.get("cooldown_on_429_s", 900.0))
        self._cooldown_until = 0.0
        # Свой лимитер: при включённом плагине частоту LinkedIn определяют только его настройки.
        self._limiter = DomainRateLimiter(default_rps=0)

    # ------------------------------------------------------------------ маршрутизация

    def resolve(self, url: str) -> tuple[str, str]:
        """(тип страницы, гостевой URL). Тип: job | search | company."""
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)

        if path.startswith("/jobs-guest/jobs/api/"):
            kind = "search" if "seeMoreJobPostings" in path else "job"
            return kind, f"https://www.linkedin.com{path}" + (f"?{parts.query}" if parts.query else "")
        if match := _JOB_VIEW_RE.match(path):
            return "job", f"{GUEST_API}/jobPosting/{match.group(1)}"
        if path.rstrip("/") in ("/jobs/search", "/jobs"):
            params = {key: query[key][0] for key in _SEARCH_PARAMS if query.get(key)}
            return "search", f"{GUEST_API}/seeMoreJobPostings/search?{urlencode(params)}"
        job_id = (query.get("currentJobId") or [""])[0]
        if path.startswith("/jobs/") and job_id.isdigit():  # например /jobs/collections/...?currentJobId=
            return "job", f"{GUEST_API}/jobPosting/{job_id}"
        if _COMPANY_RE.match(path):
            return "company", f"https://www.linkedin.com{path.rstrip('/')}"
        raise ScraperError("blocked", _UNSUPPORTED_MESSAGE, url=url)

    # ------------------------------------------------------------------ fetch

    async def fetch(self, url: str, *, render: RenderMode, use_cache: bool) -> RawPage:
        kind, target = self.resolve(url)
        key = normalize_url(target)
        if use_cache and (cached := await self.ctx.cache.get(key)) is not None:
            return cached.model_copy(update={"url": url})

        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            raise ScraperError("blocked", f"LinkedIn rate-limited us; the plugin is cooling down for "
                               f"{int(remaining)} more seconds", url=url, retryable=True)

        await self._limiter.acquire("linkedin.com", min_interval=self.min_interval, jitter=self.jitter)
        response = await self.ctx.http.get(target, rate_limit=False, retries=0)

        if response.status_code in (429, 999):
            self._cooldown_until = time.monotonic() + self.cooldown_s
            log.warning("LinkedIn вернул %s — пауза для всех запросов на %.0fс", response.status_code, self.cooldown_s)
            raise ScraperError("blocked", f"LinkedIn returned HTTP {response.status_code}; all LinkedIn requests "
                               f"are paused for {int(self.cooldown_s)}s", url=url,
                               status_code=response.status_code, retryable=True)
        if any(urlsplit(response.url).path.startswith(p) for p in _AUTHWALL_PATHS):
            raise ScraperError("blocked", "LinkedIn requires login for this page", url=url)
        raise_for_status(response.status_code, url)

        html = decode_body(response.content, response.content_type)
        if kind == "search":
            html = f"<html><body><ul>{html}</ul></body></html>"  # эндпоинт отдаёт голый фрагмент <li>
            markdown, title = self._search_markdown(html, target)
        elif kind == "job":
            markdown, title = self._job_markdown(html)
        else:
            markdown, title = None, None

        page = RawPage(
            url=url,
            final_url=target,
            status_code=response.status_code,
            content_type=response.content_type,
            body=html,
            via=f"plugin:{self.name}",
            fetched_at=time.time(),
            markdown=markdown,
            title=title,
        )
        await self.ctx.cache.put(key, page)
        return page

    # ------------------------------------------------------------------ разбор гостевой разметки

    @staticmethod
    def _search_markdown(html: str, target: str) -> tuple[str | None, str]:
        tree = LexborHTMLParser(html)
        params = parse_qs(urlsplit(target).query)
        label = " · ".join(params[k][0] for k in ("keywords", "location") if params.get(k)) or "all"
        title = f"LinkedIn job search: {label}"
        blocks: list[str] = []
        seen: set[str] = set()
        for card in tree.css("div.base-search-card"):
            job_title = _text(card.css_first(".base-search-card__title"))
            if not job_title:
                continue
            urn = card.attributes.get("data-entity-urn") or ""
            job_id = urn.rsplit(":", 1)[-1] if urn else ""
            link = card.css_first("a.base-card__full-link")
            href = f"https://www.linkedin.com/jobs/view/{job_id}" if job_id.isdigit() else (
                (link.attributes.get("href") or "").split("?", 1)[0] if link else "")
            if href in seen:  # одна вакансия — одна запись, даже если карточка повторилась
                continue
            seen.add(href)
            date_node = card.css_first("time")
            date = (date_node.attributes.get("datetime") or "") if date_node else ""
            details = [
                _text(card.css_first(".base-search-card__subtitle")),
                _text(card.css_first(".job-search-card__location")),
                f"{date} ({_text(date_node)})" if date else _text(date_node),
                _text(card.css_first(".job-posting-benefits__text")),
            ]
            blocks.append("\n".join(filter(None, [f"## {job_title}", " · ".join(filter(None, details)), href])))
        if not blocks:
            return f"# {title}\n\nNo job postings found.", title
        return f"# {title}\n\n" + "\n\n".join(blocks), title

    @staticmethod
    def _job_markdown(html: str) -> tuple[str | None, str | None]:
        tree = LexborHTMLParser(html)
        job_title = _text(tree.css_first(".top-card-layout__title, .topcard__title"))
        if not job_title:
            return None, None  # разметка изменилась — пусть работает общий разбор
        lines = [f"# {job_title}"]
        facts = {
            "Company": _text(tree.css_first(".topcard__org-name-link, .topcard__flavor a")),
            "Location": _text(tree.css_first(".topcard__flavor--bullet")),
            "Posted": _text(tree.css_first(".posted-time-ago__text")),
            "Applicants": _text(tree.css_first(".num-applicants__caption")),
        }
        lines.extend(f"**{name}:** {value}" for name, value in facts.items() if value)
        description = tree.css_first(".show-more-less-html__markup") or tree.css_first(".description__text")
        if description is not None and description.html:
            lines.append("\n## Description\n\n" + html_to_text(f"<body>{description.html}</body>"))
        criteria = []
        for item in tree.css(".description__job-criteria-item"):
            name = _text(item.css_first(".description__job-criteria-subheader"))
            value = _text(item.css_first(".description__job-criteria-text"))
            if name and value:
                criteria.append(f"- {name}: {value}")
        if criteria:
            lines.append("\n## Job criteria\n\n" + "\n".join(criteria))
        return "\n".join(lines), job_title
