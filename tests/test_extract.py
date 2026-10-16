import json

from web_scraper.llm.anthropic_provider import to_structured_schema
from web_scraper.ops.extract import parse_llm_answer
from web_scraper.scraper import Scraper

from .conftest import FakeLLM, FakeRenderer, html_response, make_config, read_fixture

LISTING_URL = "https://shop.example/catalog/"
CONTACTS_URL = "https://acme-gmbh.de/kontakt"

CARDS = {
    "heading": {"css": "h1"},
    "sku_list": {"xpath": "//div[@class='card']/@data-sku", "many": True},
    "count": {"xpath": "count(//div[@class='card'])"},
    "items": {
        "css": ".card",
        "many": True,
        "fields": {
            "name": {"css": ".name"},
            "price": {"css": ".price"},
            "url": {"css": "a.more", "attr": "href"},
        },
    },
    "missing": {"css": ".does-not-exist"},
}


async def test_selectors_extract_nested_lists(scraper, http_mock):
    http_mock.get(LISTING_URL).mock(return_value=html_response(read_fixture("listing.html")))

    envelope = await scraper.extract({"url": LISTING_URL, "mode": "selectors", "selectors": CARDS})
    assert envelope.ok, envelope.error
    data = envelope.result.data
    assert data["heading"] == "Mechanical keyboards"
    assert data["sku_list"] == ["KB-1", "KB-2", "KB-3"]
    assert data["count"] == 3.0
    assert data["missing"] is None
    assert data["items"][0] == {"name": "Alpha 75", "price": "$129", "url": "https://shop.example/catalog/alpha-75"}
    assert data["items"][1]["name"] == "Bravo TKL"  # пробелы нормализованы
    assert data["items"][2] == {"name": "Charlie 60", "price": None, "url": "https://other.example/charlie"}
    assert envelope.result.warnings == ["items[].price: no matches", "missing: no matches"]


async def test_selector_attr_html(scraper, http_mock):
    http_mock.get(LISTING_URL).mock(return_value=html_response(read_fixture("listing.html")))
    envelope = await scraper.extract(
        {"url": LISTING_URL, "mode": "selectors", "selectors": {"price": {"css": ".price", "attr": "html"}}}
    )
    assert envelope.result.data["price"] == '<span class="price">$129</span>'


async def test_bad_selectors_are_invalid_input(scraper, http_mock):
    http_mock.get(LISTING_URL).mock(return_value=html_response(read_fixture("listing.html")))
    bad_css = await scraper.extract({"url": LISTING_URL, "mode": "selectors", "selectors": {"x": {"css": "div[["}}})
    assert bad_css.error.kind == "invalid_input" and "x: invalid CSS" in bad_css.error.message

    bad_xpath = await scraper.extract({"url": LISTING_URL, "mode": "selectors", "selectors": {"x": {"xpath": "//["}}})
    assert bad_xpath.error.kind == "invalid_input"

    both = await scraper.extract(
        {"url": LISTING_URL, "mode": "selectors", "selectors": {"x": {"css": "a", "xpath": "//a"}}}
    )
    assert both.error.kind == "invalid_input"

    no_selectors = await scraper.extract({"url": LISTING_URL, "mode": "selectors"})
    assert no_selectors.error.kind == "invalid_input"


SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "emails": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["company", "emails"],
}


async def test_llm_mode_valid_answer(tmp_path, http_mock):
    http_mock.get(CONTACTS_URL).mock(return_value=html_response(read_fixture("contacts.html")))
    llm = FakeLLM(['```json\n{"company": "Acme GmbH", "emails": ["hello@acme-gmbh.de"]}\n```'])
    async with Scraper(make_config(tmp_path), browser=FakeRenderer(), llm=llm) as scraper:
        envelope = await scraper.extract(
            {"url": CONTACTS_URL, "mode": "llm", "schema": SCHEMA, "instructions": "company legal name"}
        )
    assert envelope.ok, envelope.error
    assert envelope.result.data == {"company": "Acme GmbH", "emails": ["hello@acme-gmbh.de"]}
    prompt = llm.prompts[0]
    assert "Kontakt" in prompt and "company legal name" in prompt
    assert "hello@acme-gmbh.de" in prompt  # найденные контакты передаются модели
    assert json.dumps(SCHEMA) in prompt


async def test_llm_mode_retries_invalid_answer(tmp_path, http_mock):
    http_mock.get(CONTACTS_URL).mock(return_value=html_response(read_fixture("contacts.html")))
    llm = FakeLLM(['{"company": 42}', '{"company": "Acme GmbH", "emails": []}'])
    async with Scraper(make_config(tmp_path), browser=FakeRenderer(), llm=llm) as scraper:
        envelope = await scraper.extract({"url": CONTACTS_URL, "mode": "llm", "schema": SCHEMA})
    assert envelope.ok
    assert envelope.result.data["company"] == "Acme GmbH"
    assert len(llm.prompts) == 2 and "It is invalid" in llm.prompts[1]


async def test_llm_mode_gives_up_after_retry(tmp_path, http_mock):
    http_mock.get(CONTACTS_URL).mock(return_value=html_response(read_fixture("contacts.html")))
    llm = FakeLLM(["not json at all", '{"company": null}'])
    async with Scraper(make_config(tmp_path), browser=FakeRenderer(), llm=llm) as scraper:
        envelope = await scraper.extract({"url": CONTACTS_URL, "mode": "llm", "schema": SCHEMA})
    assert envelope.error.kind == "llm_error"
    assert "does not match the schema" in envelope.error.message


async def test_llm_mode_rejects_invalid_schema(tmp_path, http_mock):
    http_mock.get(CONTACTS_URL).mock(return_value=html_response(read_fixture("contacts.html")))
    llm = FakeLLM([])
    async with Scraper(make_config(tmp_path), browser=FakeRenderer(), llm=llm) as scraper:
        envelope = await scraper.extract(
            {"url": CONTACTS_URL, "mode": "llm", "schema": {"type": "object", "properties": {"x": {"type": 5}}}}
        )
    assert envelope.error.kind == "invalid_input" and "JSON Schema" in envelope.error.message
    assert llm.prompts == []


def test_parse_llm_answer_extracts_embedded_json():
    data, error = parse_llm_answer('Here you go: {"company": "A", "emails": []} hope it helps', SCHEMA)
    assert error is None and data["company"] == "A"
    data, error = parse_llm_answer('{"company": "A"}', SCHEMA)
    assert data is None and "emails" in error


def test_structured_schema_for_claude():
    schema = {
        "type": "object",
        "properties": {
            "minimum": {"type": "integer", "minimum": 0},  # поле с именем-ключевым словом сохраняется
            "name": {"type": "string", "minLength": 2, "format": "custom-thing"},
            "tags": {"type": "array", "items": {"type": "object", "properties": {"v": {"type": "string"}}}},
        },
        "required": ["name"],
        "additionalProperties": True,
    }
    out = to_structured_schema(schema)
    assert out["additionalProperties"] is False
    assert out["properties"]["minimum"] == {"type": "integer"}
    assert out["properties"]["name"] == {"type": "string"}
    assert out["properties"]["tags"]["items"]["additionalProperties"] is False
    assert out["required"] == ["name"]
    assert to_structured_schema({"$defs": {"a": {}}, "$ref": "#/$defs/a"}) is None
