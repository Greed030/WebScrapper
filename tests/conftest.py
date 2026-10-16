"""Общие фикстуры: конфиг без задержек, фейковые браузер и LLM, мок HTTP через respx."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from web_scraper.config import Config
from web_scraper.core.browser import RenderedPage
from web_scraper.errors import ScraperError
from web_scraper.llm.base import LLMProvider
from web_scraper.scraper import Scraper

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def html_response(body: str, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **(headers or {})})


def mount_site(mock: respx.MockRouter, base: str = "https://site.test") -> respx.Route:
    """Отдаёт файлы из fixtures/site как сайт: /blog/ → blog/index.html, /about → about.html."""
    root = FIXTURES / "site"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.strip("/")
        if path == "robots.txt":
            return httpx.Response(200, text=(root / "robots.txt").read_text(), headers={"content-type": "text/plain"})
        candidates = [root / path / "index.html", root / f"{path}.html"] if path else [root / "index.html"]
        for file in candidates:
            if file.is_file():
                return html_response(file.read_text(encoding="utf-8"))
        return html_response("<html><body><h1>Not found</h1></body></html>", status=404)

    return mock.get(url__startswith=base).mock(side_effect=handler)


class FakeRenderer:
    """Подмена Playwright: отдаёт заранее заданный HTML и запоминает, что рендерили."""

    def __init__(self, pages: dict[str, str] | None = None, default: str | None = None, status: int = 200):
        self.pages = pages or {}
        self.default = default
        self.status = status
        self.calls: list[str] = []

    async def render(self, url: str, user_agent: str) -> RenderedPage:
        self.calls.append(url)
        html = self.pages.get(url, self.default)
        if html is None:
            raise ScraperError("timeout", "fake browser timeout", url=url)
        return RenderedPage(url=url, status_code=self.status, html=html, content_type="text/html")

    async def aclose(self) -> None:
        return None


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> str:
        self.prompts.append(user)
        return self.answers.pop(0)


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    data: dict[str, Any] = {
        "searxng": {"url": "http://searx.test", "default_lang": None},
        "http": {"max_retries": 2, "backoff_base": 0.001, "backoff_max": 0.01},
        "rate_limit": {"default_rps": 0, "domains": {}},
        "cache": {"path": str(tmp_path / "cache.sqlite")},
        "plugins": {"linkedin": {"enabled": False}},
    }
    for section, values in overrides.items():
        data[section] = {**data.get(section, {}), **values} if isinstance(values, dict) else values
    return Config.model_validate(data)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def renderer() -> FakeRenderer:
    return FakeRenderer()


@pytest.fixture
async def scraper(config: Config, renderer: FakeRenderer):
    instance = Scraper(config, browser=renderer)
    yield instance
    await instance.aclose()


@pytest.fixture
def http_mock():
    with respx.mock(assert_all_called=False) as mock:
        yield mock
