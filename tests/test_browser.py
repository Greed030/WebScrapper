"""Интеграционный тест с настоящим Chromium. Запуск: pytest -m browser"""

import pytest

from web_scraper.config import BrowserConfig
from web_scraper.core.browser import BrowserRenderer

pytestmark = pytest.mark.browser

JS_PAGE = (
    "data:text/html,<html><body><div id='root'></div>"
    "<script>document.getElementById('root').textContent = 'rendered by javascript';</script></body></html>"
)


async def test_renders_javascript():
    renderer = BrowserRenderer(BrowserConfig(timeout=20, wait_until="load"))
    try:
        page = await renderer.render(JS_PAGE, "Mozilla/5.0 test")
    finally:
        await renderer.aclose()
    assert "rendered by javascript" in page.html
    assert page.status_code == 200
