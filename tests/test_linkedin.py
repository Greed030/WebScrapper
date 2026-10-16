import httpx
import pytest

from web_scraper.errors import ScraperError
from web_scraper.plugins.linkedin import GUEST_API, MIN_INTERVAL_FLOOR
from web_scraper.scraper import Scraper

from .conftest import FakeRenderer, html_response, make_config, read_fixture

JOB_URL = "https://de.linkedin.com/jobs/view/senior-python-developer-at-acme-gmbh-4012345678?refId=abc"
SEARCH_URL = "https://www.linkedin.com/jobs/search?keywords=python&location=Berlin&trk=homepage"


@pytest.fixture
async def li_scraper(tmp_path):
    config = make_config(tmp_path, plugins={"linkedin": {"enabled": True, "jitter_s": 0, "min_interval_s": 0.5}})
    async with Scraper(config, browser=FakeRenderer()) as scraper:
        yield scraper


async def test_plugin_is_disabled_by_default(scraper):
    assert scraper.plugins.names == []


async def test_interval_floor(li_scraper):
    plugin = li_scraper.plugins.plugins[0]
    assert plugin.name == "linkedin"
    assert plugin.min_interval == MIN_INTERVAL_FLOOR  # 0.5 из конфига поднято до минимума


@pytest.mark.parametrize(
    ("url", "kind", "target"),
    [
        (JOB_URL, "job", f"{GUEST_API}/jobPosting/4012345678"),
        ("https://www.linkedin.com/jobs/view/4012345678/", "job", f"{GUEST_API}/jobPosting/4012345678"),
        ("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4012399999", "job",
         f"{GUEST_API}/jobPosting/4012399999"),
        (SEARCH_URL, "search", f"{GUEST_API}/seeMoreJobPostings/search?keywords=python&location=Berlin"),
        ("https://www.linkedin.com/company/acme-gmbh/", "company", "https://www.linkedin.com/company/acme-gmbh"),
    ],
)
async def test_resolve(li_scraper, url, kind, target):
    plugin = li_scraper.plugins.match(url)
    assert plugin.resolve(url) == (kind, target)


async def test_private_pages_are_refused_without_request(li_scraper, http_mock):
    route = http_mock.get(url__startswith="https://www.linkedin.com").mock(return_value=html_response("x"))
    for url in ("https://www.linkedin.com/in/someone", "https://www.linkedin.com/feed/"):
        envelope = await li_scraper.fetch({"url": url})
        assert envelope.error.kind == "blocked" and "require login" in envelope.error.message
    assert route.call_count == 0
    with pytest.raises(ScraperError):
        li_scraper.plugins.match("https://www.linkedin.com/messaging/").resolve("https://www.linkedin.com/messaging/")


async def test_search_results_markdown_and_extract(li_scraper, http_mock):
    route = http_mock.get(f"{GUEST_API}/seeMoreJobPostings/search").mock(
        return_value=html_response(read_fixture("linkedin_search.html"))
    )
    result = (await li_scraper.fetch({"url": SEARCH_URL})).result
    assert result.via == "plugin:linkedin"
    assert result.title == "LinkedIn job search: python · Berlin"
    assert "## Senior Python Developer" in result.text
    assert "Acme GmbH · Berlin, Berlin, Germany · 2026-10-10 (6 days ago) · Actively Hiring" in result.text
    assert "https://www.linkedin.com/jobs/view/4012345678" in result.text
    assert "## Backend Engineer (Python)" in result.text
    assert result.text.count("## ") == 2  # карточка с двумя классами не дублируется
    assert route.calls[0].request.url.params["keywords"] == "python"
    assert "trk" not in route.calls[0].request.url.params

    # extract работает по исходному HTML страницы, которую вернул плагин (из кэша)
    extracted = await li_scraper.extract({
        "url": SEARCH_URL, "mode": "selectors",
        "selectors": {"titles": {"css": ".base-search-card__title", "many": True}},
    })
    assert extracted.result.data == {"titles": ["Senior Python Developer", "Backend Engineer (Python)"]}
    assert route.call_count == 1


async def test_job_posting_markdown(li_scraper, http_mock):
    http_mock.get(f"{GUEST_API}/jobPosting/4012345678").mock(
        return_value=html_response(read_fixture("linkedin_job.html"))
    )
    result = (await li_scraper.fetch({"url": JOB_URL})).result
    assert result.title == "Senior Python Developer"
    assert result.text.startswith("# Senior Python Developer")
    for fragment in ("**Company:** Acme GmbH", "**Location:** Berlin, Berlin, Germany",
                     "**Applicants:** Over 200 applicants", "## Description", "- Design and build Python services",
                     "- Seniority level: Mid-Senior level", "- Employment type: Full-time"):
        assert fragment in result.text
    assert result.emails == ["careers@acme-gmbh.example"]
    assert "Show more" not in result.text


async def test_rate_limit_response_triggers_cooldown(li_scraper, http_mock):
    route = http_mock.get(url__startswith=f"{GUEST_API}/jobPosting/").mock(return_value=httpx.Response(429))
    first = await li_scraper.fetch({"url": JOB_URL})
    assert first.error.kind == "blocked" and first.error.status_code == 429

    second = await li_scraper.fetch({"url": "https://www.linkedin.com/jobs/view/4012399999"})
    assert second.error.kind == "blocked" and "cooling down" in second.error.message
    assert route.call_count == 1  # во время паузы сайт не трогаем


async def test_authwall_redirect_is_blocked(li_scraper, http_mock):
    http_mock.get("https://www.linkedin.com/company/acme-gmbh").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.linkedin.com/authwall?trk=x"})
    )
    http_mock.get("https://www.linkedin.com/authwall").mock(return_value=html_response("<h1>Sign in</h1>"))
    envelope = await li_scraper.fetch({"url": "https://www.linkedin.com/company/acme-gmbh/"})
    assert envelope.error.kind == "blocked" and "requires login" in envelope.error.message
