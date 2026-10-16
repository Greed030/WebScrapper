"""Эвристики по HTML: нужна ли отрисовка JS и не заглушка ли это антибота."""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from .parse import html_to_text

_SPA_ROOTS = ("#root", "#app", "#__next", "#__nuxt", "#___gatsby", "#svelte", "[data-reactroot]", "app-root")
_NOSCRIPT_HINTS = (
    "enable javascript",
    "javascript is required",
    "javascript is disabled",
    "requires javascript",
    "turn on javascript",
    "включите javascript",
)

# Маркеры страниц-проверок (Cloudflare, DataDome, Imperva, PerimeterX, DDoS-Guard).
_CHALLENGE_MARKERS = (
    "cf_chl_opt",
    "cf-browser-verification",
    "/cdn-cgi/challenge-platform/h/",
    "checking your browser before accessing",
    "captcha-delivery.com",
    "_incapsula_resource",
    "px-captcha",
    "ddos-guard",
)
_BLOCK_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "are you a robot",
    "verify you are human",
    "security check",
    "доступ запрещен",
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)
_SUSPICIOUS_STATUSES = {401, 403, 429, 503}


def needs_js(html: str, min_text_chars: int) -> bool:
    """True, если страница похожа на JS-оболочку: мало текста, пустой SPA-контейнер, noscript-просьба."""
    tree = LexborHTMLParser(html)
    if tree.css_first("script") is None:
        return False  # без скриптов браузер не покажет ничего сверх того, что уже есть
    text_len = len(html_to_text(html))
    if text_len < min_text_chars:
        return True
    if text_len >= min_text_chars * 3:
        return False
    for selector in _SPA_ROOTS:
        node = tree.css_first(selector)
        if node is not None and len(node.text(strip=True)) < 50:
            return True
    for node in tree.css("noscript"):
        content = node.text().lower()
        if any(hint in content for hint in _NOSCRIPT_HINTS):
            return True
    return False


def detect_block(html: str, status_code: int) -> str | None:
    """Причина блокировки или None. На 200-страницах верим только коротким страницам:
    обычные сайты тоже встраивают виджеты антиботов в формы."""
    lowered = html[:300_000].lower()
    title_match = _TITLE_RE.search(lowered)
    title = title_match.group(1) if title_match else ""
    hit = next((m for m in _CHALLENGE_MARKERS if m in lowered), None) or next(
        (t for t in _BLOCK_TITLES if t in title), None
    )
    if hit is None:
        return None
    if status_code in _SUSPICIOUS_STATUSES or len(html_to_text(html)) < 1500:
        return f"anti-bot challenge detected ({hit})"
    return None
