"""Разбор страницы: markdown основного текста, title, meta, ссылки, e-mail, телефоны."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

import phonenumbers
import trafilatura
from selectolax.lexbor import LexborHTMLParser
from trafilatura.settings import use_config

from ..models import Link, RawPage
from .urls import host_key

log = logging.getLogger(__name__)

# trafilatura по умолчанию ставит таймаут через signal, а это работает только в главном потоке.
_TRAFILATURA_CONFIG = use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

_INVISIBLE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "canvas", "head")
_BLOCK_TAGS = (
    "p, div, section, article, main, header, footer, nav, aside, table, tr, ul, ol, dl, dt, dd, "
    "blockquote, pre, figure, figcaption, form, address, fieldset, details, summary"
)
_BLOCK_BREAK, _LINE_BREAK = "", ""  # маркеры, которые не схлопнет \s+

_META_KEYS = {"description", "keywords", "author", "robots", "date", "application-name", "generator"}
_META_PREFIXES = ("og:", "twitter:", "article:")
_MAX_META = 40

_EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
_NOT_EMAIL_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "bmp", "css", "js"}
_HEADER_CHARSET_RE = re.compile(r"charset=[\"']?([\w\-:.]+)", re.I)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)

_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_TEXT_TYPES = {"application/json", "application/ld+json", "application/xml", "application/rss+xml",
               "application/atom+xml", "application/javascript"}


@dataclass
class ParsedPage:
    title: str | None
    meta: dict[str, str]
    text: str
    links: list[Link] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- декодирование


def content_kind(content_type: str | None, head: bytes) -> str | None:
    """'html', 'text' или None (бинарный/неподдерживаемый контент)."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _HTML_TYPES:
        return "html"
    if ctype.startswith("text/") or ctype in _TEXT_TYPES or ctype.endswith(("+json", "+xml")):
        return "text"
    if not ctype or ctype == "application/octet-stream":
        sniff = head[:1024].lstrip().lower()
        if sniff.startswith((b"<!doctype html", b"<html")) or b"<body" in sniff:
            return "html"
        if not ctype and b"\x00" not in head[:1024]:
            return "text"
    return None


def decode_body(content: bytes, content_type: str | None) -> str:
    """Кодировка из заголовка, затем из <meta charset>, затем UTF-8 с заменой битых байтов."""
    candidates: list[str] = []
    if content.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    if content_type and (m := _HEADER_CHARSET_RE.search(content_type)):
        candidates.append(m.group(1))
    if m := _META_CHARSET_RE.search(content[:4096]):
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- текст


def html_to_text(html: str) -> str:
    """Весь видимый текст страницы с переносами на границах блоков и markdown-заголовками.

    Используется для поиска контактов, детектора JS-страниц и как запасной вариант,
    когда trafilatura не находит основной контент (листинги, главные страницы).
    """
    tree = LexborHTMLParser(html)
    for tag in _INVISIBLE_TAGS:
        for node in tree.css(tag):
            node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    for level in range(1, 7):
        for node in root.css(f"h{level}"):
            node.insert_before(f"{_BLOCK_BREAK}{'#' * level} ")
            node.insert_after(_BLOCK_BREAK)
    for node in root.css("li"):
        node.insert_before(f"{_LINE_BREAK}- ")
    for node in root.css("br, td, th"):
        node.insert_after(_LINE_BREAK if node.tag == "br" else " ")
    for node in root.css(_BLOCK_TAGS):
        node.insert_before(_BLOCK_BREAK)
        node.insert_after(_BLOCK_BREAK)

    text = re.sub(r"\s+", " ", root.text())
    text = text.replace(_BLOCK_BREAK, "\n\n").replace(_LINE_BREAK, "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def main_text(html: str, url: str, full_text: str) -> str:
    """Основной контент в markdown через trafilatura, с откатом на весь текст страницы."""
    if len(full_text) < 1500:
        # На маленьких страницах выделять нечего, а trafilatura рвёт строки на инлайн-тегах.
        return full_text
    try:
        main = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=False,
            favor_recall=True,
            deduplicate=False,
            config=_TRAFILATURA_CONFIG,
        )
    except Exception as e:  # trafilatura иногда падает на экзотической разметке
        log.debug("trafilatura не справилась с %s: %s", url, e)
        main = None
    # trafilatura оставляет сноски вида <sup>[1]</sup> как HTML — в тексте для агента это шум.
    main = re.sub(r"</?(?:sup|sub)>", "", main or "").strip()
    # Короткий «основной контент» на большой странице — признак листинга или лендинга:
    # trafilatura выбрала один блок, а агенту нужно всё.
    if not main or (len(main) < 1500 and len(main) < 0.3 * len(full_text)):
        return full_text
    return main


# --------------------------------------------------------------------------- метаданные и ссылки


def _collapse(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _base_url(tree: LexborHTMLParser, page_url: str) -> str:
    base = tree.css_first("base[href]")
    href = base.attributes.get("href") if base else None
    if href:
        try:
            return urljoin(page_url, href.strip())
        except ValueError:
            pass
    return page_url


def _title(tree: LexborHTMLParser) -> str | None:
    node = tree.css_first("title")
    title = _collapse(node.text()) if node else ""
    return title or None


def _meta(tree: LexborHTMLParser, base: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    html_node = tree.css_first("html")
    lang = html_node.attributes.get("lang") if html_node else None
    if lang:
        meta["lang"] = lang.strip()
    for node in tree.css("meta[content]"):
        attrs = node.attributes
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = _collapse(attrs.get("content"))
        if not key or not content or len(meta) >= _MAX_META:
            continue
        if key in _META_KEYS or key.startswith(_META_PREFIXES):
            meta.setdefault(key, content[:500])
    canonical = tree.css_first('link[rel="canonical"][href]')
    if canonical:
        try:
            meta["canonical"] = urljoin(base, canonical.attributes["href"].strip())
        except ValueError:
            pass
    return meta


def _links(tree: LexborHTMLParser, base: str) -> tuple[list[Link], list[str], list[str]]:
    """Возвращает (http-ссылки, адреса из mailto:, номера из tel:)."""
    page_host = host_key(base)
    links: list[Link] = []
    index: dict[str, int] = {}
    mailtos: list[str] = []
    tels: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        lowered = href.lower()
        if lowered.startswith("mailto:"):
            mailtos.append(href[7:])
            continue
        if lowered.startswith("tel:"):
            tels.append(href[4:])
            continue
        try:
            absolute = urljoin(base, href).split("#", 1)[0]
            parts = urlsplit(absolute)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        text = _collapse(a.text(separator=" "))[:200] or _collapse(
            a.attributes.get("title") or a.attributes.get("aria-label")
        )[:200]
        if absolute in index:
            existing = links[index[absolute]]
            if not existing.text and text:
                existing.text = text
            continue
        index[absolute] = len(links)
        links.append(Link(url=absolute, text=text, internal=host_key(absolute) == page_host))
    return links, mailtos, tels


# --------------------------------------------------------------------------- контакты


def find_emails(text: str, mailtos: list[str] | None = None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        email = candidate.strip().strip(".").lower()
        if not _EMAIL_RE.fullmatch(email) or email.rsplit(".", 1)[-1] in _NOT_EMAIL_TLDS:
            return
        if email not in seen:
            seen.add(email)
            found.append(email)

    for raw in mailtos or []:
        for part in unquote(raw.split("?", 1)[0]).split(","):
            add(part)
    for match in _EMAIL_RE.finditer(text):
        add(match.group(0))
    return found[:50]


def find_phones(text: str, tels: list[str] | None = None, region: str | None = None) -> list[str]:
    """Номера в E.164. Из tel:-ссылок берём всё правдоподобное, из текста — только валидные номера."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value not in seen:
            seen.add(value)
            found.append(value)

    for raw in tels or []:
        candidate = unquote(raw.split("?", 1)[0]).strip()
        try:
            number = phonenumbers.parse(candidate, region)
            if phonenumbers.is_possible_number(number):
                add(phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164))
                continue
        except phonenumbers.NumberParseException:
            pass
        cleaned = re.sub(r"[^\d+]", "", candidate)
        if len(re.sub(r"\D", "", cleaned)) >= 6:
            add(cleaned)

    for match in phonenumbers.PhoneNumberMatcher(text[:200_000], region, leniency=phonenumbers.Leniency.VALID):
        add(phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164))
    return found[:50]


# --------------------------------------------------------------------------- точка входа


def parse_links(page: RawPage) -> list[Link]:
    if not page.is_html:
        return []
    tree = LexborHTMLParser(page.body)
    return _links(tree, _base_url(tree, page.final_url))[0]


def parse_page(page: RawPage, *, phone_region: str | None = None) -> ParsedPage:
    if not page.is_html:
        text = (page.markdown or page.body).strip()
        return ParsedPage(
            title=page.title,
            meta={},
            text=text,
            emails=find_emails(text),
            phones=find_phones(text, region=phone_region),
        )

    tree = LexborHTMLParser(page.body)
    base = _base_url(tree, page.final_url)
    meta = _meta(tree, base)
    links, mailtos, tels = _links(tree, base)
    full_text = html_to_text(page.body)
    if page.markdown is not None:
        text = page.markdown.strip()
    else:
        text = main_text(page.body, page.final_url, full_text)
    return ParsedPage(
        title=page.title or _title(tree) or meta.get("og:title"),
        meta=meta,
        text=text,
        links=links,
        emails=find_emails(full_text, mailtos),
        phones=find_phones(full_text, tels, phone_region),
    )
