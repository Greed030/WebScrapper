"""Мелкие утилиты для URL: проверка, нормализация, сравнение доменов."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def validate_http_url(url: str) -> str:
    """Добавляет https:// при отсутствии схемы и проверяет, что это абсолютный http(s) URL."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"expected an absolute http(s) URL, got {url!r}")
    return url


def normalize_url(url: str) -> str:
    """Ключ для кэша и дедупликации: схема/хост в нижнем регистре, без фрагмента и порта по умолчанию."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def host_key(url_or_host: str) -> str:
    """Хост без www. в нижнем регистре: «example.com» и для URL, и для голого хоста."""
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, domain: str) -> bool:
    """True, если host — это domain или его поддомен."""
    host, domain = host_key(host), host_key(domain)
    return host == domain or host.endswith("." + domain)
