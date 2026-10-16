from web_scraper.core.detect import detect_block, needs_js
from web_scraper.core.parse import content_kind, decode_body, find_phones, html_to_text, parse_page
from web_scraper.models import RawPage

from .conftest import read_fixture


def page(name: str, url: str) -> RawPage:
    return RawPage(url=url, final_url=url, status_code=200, body=read_fixture(name), fetched_at=0)


def test_article_metadata_and_links():
    parsed = parse_page(page("article.html", "https://northwind.example/blog/build-times"))

    assert parsed.title == "How We Cut Our Build Times in Half | Northwind Engineering Blog"
    assert parsed.meta["description"].startswith("A practical walkthrough")
    assert parsed.meta["og:type"] == "article"
    assert parsed.meta["lang"] == "en"
    assert parsed.meta["canonical"] == "https://northwind.example/blog/build-times"

    urls = [link.url for link in parsed.links]
    assert "https://northwind.example/about" in urls  # фрагмент #team отброшен
    assert "https://northwind.example/blog/ci-caching-deep-dive" in urls
    assert not any(u.startswith(("javascript:", "mailto:", "tel:")) for u in urls)
    assert len(urls) == len(set(urls))
    external = next(link for link in parsed.links if link.url.startswith("https://github.com"))
    assert external.internal is False and external.text == "GitHub"


def test_article_main_text_is_clean_markdown():
    text = parse_page(page("article.html", "https://northwind.example/blog/build-times")).text

    assert text.startswith("# How We Cut Our Build Times in Half")
    assert "## 2. Run independent jobs in parallel" in text
    assert "- Unit tests: four shards" in text
    assert "Cookie settings" not in text  # футер и навигация не попадают в основной текст
    assert "window.dataLayer" not in text


def test_footnote_tags_are_removed_from_main_text():
    paragraph = "Web scraping extracts data from websites using automated software agents. " * 12
    html = (f"<html><body><article><h1>Scraping</h1><p>{paragraph}<sup>[1]</sup></p>"
            f"<p>{paragraph}</p><p>{paragraph}</p></article></body></html>")
    text = parse_page(RawPage(url="https://w.test/a", final_url="https://w.test/a", status_code=200,
                              body=html, fetched_at=0)).text
    assert "Web scraping extracts data" in text
    assert "<sup>" not in text and "</sup>" not in text


def test_contacts_found_across_whole_page():
    parsed = parse_page(page("article.html", "https://northwind.example/blog/build-times"))
    assert parsed.emails == ["press@northwind.example", "engineering@northwind.example"]
    assert parsed.phones == ["+12125550147"]


def test_contacts_page_emails_and_phones():
    parsed = parse_page(page("contacts.html", "https://acme-gmbh.de/kontakt"))

    assert parsed.emails == [
        "support@acme-gmbh.de",
        "billing@acme-gmbh.de",
        "hello@acme-gmbh.de",
        "jobs@acme-gmbh.de",
    ]  # logo@2x.png и адрес из <script> не считаются
    assert "+493090182000" in parsed.phones  # из tel:
    assert "+4930901820" in parsed.phones
    assert "+14155550132" in parsed.phones
    assert not any("20260314" in p or "4711" in p for p in parsed.phones)


def test_local_phone_numbers_need_region():
    text = "Fax: 030 9018 2111"
    assert find_phones(text) == []
    assert find_phones(text, region="DE") == ["+493090182111"]


def test_small_page_returns_full_visible_text():
    parsed = parse_page(page("listing.html", "https://shop.example/catalog/"))
    for name in ("Alpha 75", "Bravo TKL", "Charlie 60", "$129"):
        assert name in parsed.text
    # <base href> учитывается при разрешении ссылок
    assert "https://shop.example/catalog/alpha-75" in [link.url for link in parsed.links]


def test_html_to_text_structure():
    text = html_to_text("<body><h2>Title</h2><p>One <b>bold</b> word</p><ul><li>a</li><li>b</li></ul>"
                        "<script>bad()</script></body>")
    assert text == "## Title\n\nOne bold word\n\n- a\n- b"


def test_plain_text_page():
    raw = RawPage(url="https://x.test/a.txt", final_url="https://x.test/a.txt", status_code=200,
                  body="hello\nmail me: a@b.example", is_html=False, fetched_at=0)
    parsed = parse_page(raw)
    assert parsed.text == "hello\nmail me: a@b.example"
    assert parsed.emails == ["a@b.example"]
    assert parsed.links == []


def test_decode_body_respects_charsets():
    cyrillic = "<html><head><meta charset='windows-1251'></head><body>Привет</body></html>"
    assert "Привет" in decode_body(cyrillic.encode("cp1251"), "text/html")
    assert decode_body("Grüße".encode("latin-1"), "text/plain; charset=ISO-8859-1") == "Grüße"
    assert decode_body(b"\xff\xfebroken", None).endswith("broken")


def test_content_kind():
    assert content_kind("text/html; charset=utf-8", b"") == "html"
    assert content_kind("application/json", b"{}") == "text"
    assert content_kind("application/pdf", b"%PDF-1.7") is None
    assert content_kind(None, b"<!DOCTYPE html><html>") == "html"
    assert content_kind("application/octet-stream", b"\x00\x01binary") is None


def test_needs_js_detects_spa_shell():
    assert needs_js(read_fixture("spa_shell.html"), 300) is True
    assert needs_js(read_fixture("article.html"), 300) is False
    # Короткая страница без скриптов: браузер ничего не добавит
    assert needs_js("<html><body><p>Tiny static page</p></body></html>", 300) is False


def test_detect_block():
    assert detect_block(read_fixture("cloudflare.html"), 403) is not None
    assert detect_block(read_fixture("cloudflare.html"), 200) is not None  # короткая страница с маркером
    assert detect_block(read_fixture("article.html"), 200) is None
    assert detect_block(read_fixture("article.html"), 403) is None
