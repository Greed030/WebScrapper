"""Загрузка config.yaml в типизированную Pydantic-модель."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

CONFIG_ENV = "WEB_SCRAPER_CONFIG"

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36",
]

# Проекты Wikimedia по своей robot policy отдают 403 ботам, которые притворяются браузером,
# и пускают тех, кто честно представляется.
BOT_USER_AGENT = "web-scraper/0.1 (+https://github.com/Greed030/WebScrapper)"
DEFAULT_USER_AGENT_OVERRIDES = {
    domain: BOT_USER_AGENT
    for domain in (
        "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org", "wikibooks.org", "wikiquote.org",
        "wikisource.org", "wikivoyage.org", "wikinews.org", "wikiversity.org", "mediawiki.org",
    )
}


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class SearxngConfig(BaseModel):
    url: str = "http://localhost:8080"
    timeout: float = 15.0
    default_lang: str | None = "en"
    default_region: str | None = None
    max_pages: int = Field(3, ge=1, description="сколько страниц выдачи SearXNG можно догрузить")


class HttpConfig(BaseModel):
    timeout: float = 20.0
    connect_timeout: float = 10.0
    max_retries: int = Field(3, ge=0)
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    max_response_bytes: int = 5_000_000
    proxy: str | None = None
    verify_ssl: bool = True
    accept_language: str = "en-US,en;q=0.9"
    user_agents: list[str] = Field(default_factory=lambda: list(DEFAULT_USER_AGENTS), min_length=1)
    user_agent_overrides: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_USER_AGENT_OVERRIDES),
        description="домен → фиксированный User-Agent вместо ротации (покрывает поддомены)",
    )


class BrowserConfig(BaseModel):
    enabled: bool = True
    headless: bool = True
    timeout: float = 30.0
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "networkidle"
    min_text_chars: int = Field(300, description="меньше видимого текста — считаем страницу JS-оболочкой")
    max_concurrent: int = Field(2, ge=1)


class RateLimitConfig(BaseModel):
    default_rps: float = Field(1.0, description="запросов в секунду на домен; 0 — без ограничения")
    domains: dict[str, float] = Field(default_factory=lambda: {"linkedin.com": 0.1})


class CacheConfig(BaseModel):
    enabled: bool = True
    path: str = "data/cache.sqlite"
    ttl_seconds: int = 86_400


class OutputConfig(BaseModel):
    max_chars: int = Field(20_000, description="текст страницы в fetch по умолчанию")
    max_chars_limit: int = Field(100_000, description="потолок для max_chars из запроса")
    max_links: int = 200
    page_chars_in_batch: int = Field(3_000, description="текст одной страницы в crawl/search_and_fetch")
    max_total_chars: int = Field(60_000, description="суммарный текст ответа crawl/search_and_fetch")


class ContactsConfig(BaseModel):
    phone_region: str | None = Field(
        None, description="регион для номеров без +кода (например DE); None — только международный формат"
    )


class CrawlConfig(BaseModel):
    max_depth_cap: int = 5
    max_pages_cap: int = 100
    concurrency: int = Field(4, ge=1)
    respect_robots: bool = True


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "openai", "ollama"] = "anthropic"
    model: str = "claude-haiku-5-5"
    api_key_env: str | None = "ANTHROPIC_API_KEY"
    base_url: str | None = None
    max_input_chars: int = 40_000
    max_tokens: int = 16_000
    timeout: float = 120.0
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        "low", description="только для anthropic: output_config.effort"
    )


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    searxng: SearxngConfig = Field(default_factory=SearxngConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    contacts: ContactsConfig = Field(default_factory=ContactsConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=lambda: {"linkedin": {"enabled": False}})
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Читает YAML: явный путь → $WEB_SCRAPER_CONFIG → ./config.yaml → значения по умолчанию.

    Относительный cache.path считается от папки конфига.
    """
    candidate = Path(path or os.environ.get(CONFIG_ENV) or "config.yaml")
    if not candidate.exists():
        if path or os.environ.get(CONFIG_ENV):
            raise FileNotFoundError(f"Конфиг не найден: {candidate}")
        log.warning("config.yaml не найден, используются значения по умолчанию")
        return Config()

    data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    config = Config.model_validate(data)
    cache_path = Path(config.cache.path)
    if not cache_path.is_absolute():
        config.cache.path = str((candidate.parent / cache_path).resolve())
    return config
