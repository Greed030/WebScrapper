"""HTTP API, tool definitions, клиент ScraperTools, CLI и MCP."""

import json

import httpx
import jsonschema
import pytest

from web_scraper import cli
from web_scraper.api import create_app
from web_scraper.mcp_server import build_server
from web_scraper.tools import ScraperTools, tool_definitions

from .conftest import html_response, read_fixture

ARTICLE_URL = "https://northwind.example/blog/build-times"


@pytest.fixture
async def api(scraper):
    transport = httpx.ASGITransport(app=create_app(scraper=scraper))
    async with httpx.AsyncClient(transport=transport, base_url="http://scraper.test") as client:
        yield client


async def test_api_fetch(api, http_mock):
    http_mock.get(ARTICLE_URL).mock(return_value=html_response(read_fixture("article.html")))
    response = await api.post("/fetch", json={"url": ARTICLE_URL, "max_chars": 1000})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and "error" not in body  # None-поля не отдаются
    assert body["result"]["via"] == "httpx" and body["result"]["truncated"] is True


async def test_api_site_errors_are_200_with_envelope(api, http_mock):
    http_mock.get("https://x.test/gone").mock(return_value=html_response("gone", status=410))
    response = await api.post("/fetch", json={"url": "https://x.test/gone"})
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": {"kind": "not_found", "message": "HTTP 410", "url": "https://x.test/gone", "status_code": 410,
                  "retryable": False},
    }


async def test_api_validation_errors_use_envelope(api):
    response = await api.post("/crawl", json={"start_url": "https://a.test", "max_pages": 0, "extra": 1})
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False and body["error"]["kind"] == "invalid_input"
    assert "max_pages" in body["error"]["message"]


async def test_api_tools_and_health(api):
    tools = (await api.get("/tools")).json()
    assert [tool["name"] for tool in tools] == [
        "scraper_search", "scraper_fetch", "scraper_extract", "scraper_crawl", "scraper_search_and_fetch",
    ]
    health = (await api.get("/health")).json()
    assert health["ok"] is True and health["plugins"] == []


def test_tool_definitions_are_valid_json_schema():
    for tool in tool_definitions():
        assert set(tool) == {"name", "description", "input_schema"}
        assert len(tool["description"]) > 80
        schema = tool["input_schema"]
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert '"title"' not in json.dumps({k: v for k, v in schema.items() if k != "properties"})

    by_name = {tool["name"]: tool["input_schema"] for tool in tool_definitions()}
    assert by_name["scraper_fetch"]["required"] == ["url"]
    assert by_name["scraper_fetch"]["properties"]["max_chars"] == {
        "description": "Max characters of page text to return. Server default if omitted.",
        "minimum": 500,
        "type": "integer",
    }
    assert "schema" in by_name["scraper_extract"]["properties"]
    jsonschema.validate({"url": "https://a.test", "mode": "llm", "schema": {"type": "object"}},
                        by_name["scraper_extract"])


async def test_scraper_tools_client(scraper, http_mock):
    http_mock.get(ARTICLE_URL).mock(return_value=html_response(read_fixture("article.html")))
    transport = httpx.ASGITransport(app=create_app(scraper=scraper))
    tools = ScraperTools("http://scraper.test", client=httpx.AsyncClient(transport=transport))

    block = {"type": "tool_use", "id": "toolu_1", "name": "scraper_fetch", "input": {"url": ARTICLE_URL}}
    assert tools.handles(block) and not tools.handles({"type": "tool_use", "name": "other"})
    result = await tools.tool_result(block)
    assert result["tool_use_id"] == "toolu_1" and result["is_error"] is False
    assert json.loads(result["content"])["result"]["title"].startswith("How We Cut")

    failed = await tools.tool_result({"type": "tool_use", "id": "toolu_2", "name": "scraper_fetch",
                                      "input": {"url": "not a url at all"}})
    assert failed["is_error"] is True

    unknown = json.loads(await tools.run("scraper_unknown", {}))
    assert unknown["error"]["kind"] == "invalid_input"
    await tools.aclose()


async def test_scraper_tools_unreachable_server():
    tools = ScraperTools("http://127.0.0.1:9")
    content = json.loads(await tools.run("scraper_search", {"query": "x"}))
    assert content["ok"] is False and content["error"]["kind"] == "network"
    await tools.aclose()


async def test_mcp_server_builds(scraper):
    server = build_server(scraper)
    assert server.name == "web-scraper"


def test_cli_payloads():
    parser = cli.build_parser()
    op, payload = cli._payload(parser.parse_args(["fetch", "https://a.test", "--max-chars", "800", "--no-cache"]))
    assert op == "fetch" and payload == {"url": "https://a.test", "max_chars": 800, "use_cache": False}

    op, payload = cli._payload(parser.parse_args(["extract", "https://a.test", "--selectors", '{"t": {"css": "h1"}}']))
    assert payload["mode"] == "selectors" and payload["selectors"] == {"t": {"css": "h1"}}

    op, payload = cli._payload(parser.parse_args(["crawl", "https://a.test", "--pattern", "/blog/", "--any-domain"]))
    assert op == "crawl" and payload == {"start_url": "https://a.test", "url_pattern": "/blog/", "same_domain": False}


def test_cli_tools_command(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # без config.yaml — значения по умолчанию
    assert cli.main(["tools"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "scraper_search"
