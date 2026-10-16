import json

import httpx

from web_scraper.ops.search import searxng_language

from .conftest import html_response, read_fixture

SEARCH = "http://searx.test/search"


def searx_json(name: str) -> httpx.Response:
    return httpx.Response(200, json=json.loads(read_fixture(name)))


async def test_search_parses_and_dedupes(scraper, http_mock):
    pages = [searx_json("searxng_page1.json"), searx_json("searxng_page2.json")]
    route = http_mock.get(SEARCH).mock(side_effect=pages)

    request = {"query": "python developer berlin", "num_results": 3, "lang": "de", "region": "de"}
    envelope = await scraper.search(request)
    assert envelope.ok, envelope.error
    hits = envelope.result.results
    assert [hit.url for hit in hits] == [
        "https://jobs.example/python-berlin",
        "https://blog.example/python-in-berlin",
        "https://careers.example/backend",  # дубль с #apply и magnet: отброшены, добрали со второй страницы
    ]
    assert hits[0].title == "Python Developer (m/w/d) – Berlin"
    assert hits[0].snippet == "We are hiring a Python developer in Berlin."
    assert hits[0].engine == "duckduckgo"

    params = route.calls[0].request.url.params
    assert params["format"] == "json" and params["language"] == "de-DE" and params["pageno"] == "1"
    assert route.calls[1].request.url.params["pageno"] == "2"


async def test_search_stops_when_enough(scraper, http_mock):
    route = http_mock.get(SEARCH).mock(return_value=searx_json("searxng_page1.json"))
    envelope = await scraper.search({"query": "q", "num_results": 2})
    assert len(envelope.result.results) == 2
    assert route.call_count == 1
    assert "language" not in route.calls[0].request.url.params


def test_searxng_language():
    assert searxng_language("de", "at") == "de-AT"
    assert searxng_language("EN", None) == "en"
    assert searxng_language(None, "DE") == "en-DE"
    assert searxng_language(None, None) is None


async def test_search_explains_disabled_json_format(scraper, http_mock):
    http_mock.get(SEARCH).mock(return_value=httpx.Response(403, text="Forbidden"))
    envelope = await scraper.search({"query": "q"})
    assert envelope.error.kind == "http_error" and "search.formats" in envelope.error.message


async def test_search_unreachable(scraper, http_mock):
    http_mock.get(SEARCH).mock(side_effect=httpx.ConnectError("connection refused"))
    envelope = await scraper.search({"query": "q"})
    assert envelope.error.kind == "network" and "SearXNG is unreachable" in envelope.error.message


async def test_search_and_fetch(scraper, http_mock):
    http_mock.get(SEARCH).mock(return_value=searx_json("searxng_page1.json"))
    http_mock.get("https://jobs.example/python-berlin").mock(return_value=html_response(read_fixture("article.html")))
    http_mock.get("https://blog.example/python-in-berlin").mock(return_value=html_response("gone", status=404))

    envelope = await scraper.search_and_fetch({"query": "python developer berlin", "n": 2, "max_chars_per_page": 300})
    assert envelope.ok, envelope.error
    first, second = envelope.result.items
    assert first.rank == 1 and first.page.error is None
    assert first.page.truncated is True and len(first.page.text) <= 300
    assert first.page.emails == ["press@northwind.example", "engineering@northwind.example"]
    assert second.page.error.kind == "not_found"
    assert second.title == "Python in Berlin"
