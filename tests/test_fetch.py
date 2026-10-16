import httpx

from web_scraper.ops.fetch import slice_text
from web_scraper.scraper import Scraper

from .conftest import FakeRenderer, html_response, make_config, read_fixture

ARTICLE_URL = "https://northwind.example/blog/build-times"


async def test_fetch_article_and_cache(scraper, http_mock):
    route = http_mock.get(ARTICLE_URL).mock(return_value=html_response(read_fixture("article.html")))

    first = await scraper.fetch({"url": ARTICLE_URL})
    assert first.ok, first.error
    result = first.result
    assert result.via == "httpx" and result.from_cache is False
    assert result.title.startswith("How We Cut Our Build Times")
    assert result.emails == ["press@northwind.example", "engineering@northwind.example"]
    assert result.truncated is False and result.next_offset is None
    assert result.links_total == len(result.links) > 5

    second = await scraper.fetch({"url": ARTICLE_URL})
    assert second.result.from_cache is True
    assert second.result.text == result.text
    assert route.call_count == 1


async def test_truncation_and_offset(scraper, http_mock):
    route = http_mock.get(ARTICLE_URL).mock(return_value=html_response(read_fixture("article.html")))

    first = (await scraper.fetch({"url": ARTICLE_URL, "max_chars": 600})).result
    assert first.truncated is True
    assert len(first.text) <= 600
    assert first.total_chars > 600

    second = (await scraper.fetch({"url": ARTICLE_URL, "max_chars": 600, "offset": first.next_offset})).result
    assert second.offset == first.next_offset
    assert second.links == [] and second.links_total == first.links_total  # ссылки не повторяются
    assert second.text and second.text not in first.text
    assert route.call_count == 1  # второй кусок — из кэша


def test_slice_text_cuts_on_boundaries():
    text = "A" * 30 + "\n\n" + "B" * 30
    chunk, next_offset = slice_text(text, 0, 34)  # граница абзаца в последних 20% куска
    assert chunk == "A" * 30
    assert text[next_offset:] == "B" * 30

    words = "alpha beta gamma delta"
    chunk, next_offset = slice_text(words, 0, 12)
    assert chunk == "alpha beta"
    assert words[next_offset:] == "gamma delta"

    assert slice_text(text, len(text), 40) == ("", None)
    assert slice_text(text, 0, 1000) == (text, None)


async def test_error_kinds(scraper, http_mock):
    http_mock.get("https://x.test/missing").mock(return_value=html_response("<h1>404</h1>", status=404))
    http_mock.get("https://x.test/forbidden").mock(return_value=html_response("<h1>No</h1>", status=403))
    http_mock.get("https://x.test/file.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})
    )
    http_mock.get("https://nowhere.test/").mock(side_effect=httpx.ConnectError("[Errno 11001] getaddrinfo failed"))

    cases = {
        "https://x.test/missing": "not_found",
        "https://x.test/forbidden": "blocked",
        "https://x.test/file.pdf": "unsupported_content",
        "https://nowhere.test/": "not_found",
    }
    for url, kind in cases.items():
        envelope = await scraper.fetch({"url": url})
        assert envelope.ok is False
        assert envelope.error.kind == kind, (url, envelope.error)
        assert envelope.error.url == url


async def test_timeout_is_retried_then_reported(scraper, http_mock):
    route = http_mock.get("https://slow.test/").mock(side_effect=httpx.ReadTimeout("slow"))
    envelope = await scraper.fetch({"url": "https://slow.test/"})
    assert envelope.error.kind == "timeout" and envelope.error.retryable is True
    assert route.call_count == 3  # 1 + max_retries(2)


async def test_retry_on_503_then_success(scraper, http_mock):
    route = http_mock.get("https://flaky.test/").mock(
        side_effect=[
            html_response("busy", status=503, headers={"retry-after": "0"}),
            html_response(read_fixture("article.html")),
        ]
    )
    envelope = await scraper.fetch({"url": "https://flaky.test/"})
    assert envelope.ok
    assert route.call_count == 2


async def test_spa_falls_back_to_browser(scraper, renderer, http_mock):
    url = "https://app.test/"
    http_mock.get(url).mock(return_value=html_response(read_fixture("spa_shell.html")))
    renderer.pages[url] = read_fixture("article.html")

    envelope = await scraper.fetch({"url": url})
    assert envelope.result.via == "playwright"
    assert "Run independent jobs" in envelope.result.text
    assert renderer.calls == [url]


async def test_render_never_and_always(scraper, renderer, http_mock):
    url = "https://app.test/"
    route = http_mock.get(url).mock(return_value=html_response(read_fixture("spa_shell.html")))
    renderer.pages[url] = read_fixture("article.html")

    never = await scraper.fetch({"url": url, "render": "never", "use_cache": False})
    assert never.result.via == "httpx" and renderer.calls == []

    always = await scraper.fetch({"url": url, "render": "always", "use_cache": False})
    assert always.result.via == "playwright"
    assert route.call_count == 1  # always идёт сразу в браузер


async def test_browser_failure_keeps_httpx_result(scraper, http_mock):
    url = "https://app.test/"
    http_mock.get(url).mock(return_value=html_response(read_fixture("spa_shell.html")))
    envelope = await scraper.fetch({"url": url})  # FakeRenderer без страницы падает с timeout
    assert envelope.ok and envelope.result.via == "httpx"


async def test_challenge_page_tries_browser_then_blocks(scraper, renderer, http_mock):
    url = "https://shop.example/"
    http_mock.get(url).mock(return_value=html_response(read_fixture("cloudflare.html"), status=403))

    renderer.pages[url] = read_fixture("cloudflare.html")
    blocked = await scraper.fetch({"url": url})
    assert blocked.error.kind == "blocked" and "anti-bot" in blocked.error.message

    renderer.pages[url] = read_fixture("article.html")
    passed = await scraper.fetch({"url": url})
    assert passed.ok and passed.result.via == "playwright"


async def test_challenge_without_browser_is_blocked(tmp_path, http_mock):
    url = "https://shop.example/"
    http_mock.get(url).mock(return_value=html_response(read_fixture("cloudflare.html"), status=403))
    async with Scraper(make_config(tmp_path), browser=None) as scraper:
        envelope = await scraper.fetch({"url": url})
    assert envelope.error.kind == "blocked" and envelope.error.status_code == 403


async def test_invalid_input_is_reported_not_raised(scraper):
    bad_url = await scraper.fetch({"url": "ftp://example.com/file"})
    assert bad_url.error.kind == "invalid_input" and "url" in bad_url.error.message

    unknown_field = await scraper.fetch({"url": "https://example.com", "colour": "red"})
    assert unknown_field.error.kind == "invalid_input"

    unknown_op = await scraper.call("delete_internet", {})
    assert unknown_op.error.kind == "invalid_input"


async def test_url_without_scheme_gets_https(scraper, http_mock):
    http_mock.get("https://example.com/").mock(return_value=html_response("<html><body><p>Hi</p></body></html>"))
    envelope = await scraper.fetch({"url": "example.com/"})
    assert envelope.ok and envelope.result.url == "https://example.com/"


async def test_user_agent_override_for_wikimedia(scraper, http_mock):
    wiki = http_mock.get("https://en.wikipedia.org/wiki/Web_scraping").mock(
        return_value=html_response(read_fixture("article.html"))
    )
    other = http_mock.get(ARTICLE_URL).mock(return_value=html_response(read_fixture("article.html")))
    await scraper.fetch({"url": "https://en.wikipedia.org/wiki/Web_scraping"})
    await scraper.fetch({"url": ARTICLE_URL})
    assert wiki.calls[0].request.headers["user-agent"].startswith("web-scraper/")
    assert other.calls[0].request.headers["user-agent"].startswith("Mozilla/5.0")


def test_browser_download_is_unsupported_content():
    from playwright.async_api import Error as PlaywrightError

    from web_scraper.core.browser import _map_browser_error

    error = _map_browser_error(PlaywrightError("Page.goto: Download is starting"), "https://x.test/a.pdf")
    assert error.kind == "unsupported_content"


async def test_cache_disabled_by_request(tmp_path, http_mock):
    route = http_mock.get(ARTICLE_URL).mock(return_value=html_response(read_fixture("article.html")))
    async with Scraper(make_config(tmp_path), browser=FakeRenderer()) as scraper:
        await scraper.fetch({"url": ARTICLE_URL})
        fresh = await scraper.fetch({"url": ARTICLE_URL, "use_cache": False})
    assert fresh.result.from_cache is False
    assert route.call_count == 2
