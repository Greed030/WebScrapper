from web_scraper.scraper import Scraper

from .conftest import FakeRenderer, make_config, mount_site

START = "https://site.test/"


async def test_crawl_follows_internal_links_by_depth(scraper, http_mock):
    mount_site(http_mock)
    envelope = await scraper.crawl({"start_url": START, "max_depth": 1, "max_pages": 50})
    assert envelope.ok, envelope.error
    result = envelope.result

    by_url = {page.url: page for page in result.pages}
    assert set(by_url) == {START, "https://site.test/about", "https://site.test/blog/", "https://site.test/contact"}
    assert by_url[START].depth == 0 and by_url["https://site.test/about"].depth == 1
    assert by_url["https://site.test/contact"].emails == ["team@site.test"]
    assert by_url["https://site.test/contact"].phones == ["+442079460958"]
    # robots.txt запрещает /private/, PDF и внешние ссылки не обходятся
    assert result.skipped_by_robots == 1
    assert not any("external.example" in url or url.endswith(".pdf") for url in by_url)
    assert result.stop_reason == "completed"


async def test_crawl_reports_failed_pages(scraper, http_mock):
    mount_site(http_mock)
    result = (await scraper.crawl({"start_url": "https://site.test/about", "max_depth": 1})).result
    team = next(page for page in result.pages if page.url == "https://site.test/team")
    assert team.error.kind == "not_found"
    assert result.pages_failed == 1 and result.pages_ok == 2


async def test_crawl_url_pattern(scraper, http_mock):
    mount_site(http_mock)
    result = (await scraper.crawl({"start_url": START, "max_depth": 3, "url_pattern": r"/blog/"})).result
    urls = {page.url for page in result.pages}
    assert urls == {START, "https://site.test/blog/", "https://site.test/blog/post-1", "https://site.test/blog/post-2"}


async def test_crawl_max_pages(scraper, http_mock):
    mount_site(http_mock)
    result = (await scraper.crawl({"start_url": START, "max_depth": 3, "max_pages": 2})).result
    assert len(result.pages) == 2
    assert result.stop_reason == "max_pages"


async def test_crawl_caps_and_char_budget(tmp_path, http_mock):
    mount_site(http_mock)
    config = make_config(tmp_path, crawl={"max_depth_cap": 1}, output={"max_total_chars": 60})
    async with Scraper(config, browser=FakeRenderer()) as scraper:
        result = (await scraper.crawl({"start_url": START, "max_depth": 4})).result
    assert "max_depth limited to 1 by server config" in result.warnings
    assert sum(len(page.text) for page in result.pages) <= 60
    assert result.stop_reason == "char_budget"
    assert any(page.truncated for page in result.pages)


async def test_crawl_without_robots(tmp_path, http_mock):
    mount_site(http_mock)
    config = make_config(tmp_path, crawl={"respect_robots": False})
    async with Scraper(config, browser=FakeRenderer()) as scraper:
        result = (await scraper.crawl({"start_url": START, "max_depth": 1})).result
    assert result.skipped_by_robots == 0
    assert "https://site.test/private/secret" in {page.url for page in result.pages}


async def test_crawl_rejects_bad_regex(scraper):
    envelope = await scraper.crawl({"start_url": START, "url_pattern": "(unclosed"})
    assert envelope.error.kind == "invalid_input" and "regex" in envelope.error.message
