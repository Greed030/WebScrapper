import time

from web_scraper.core.cache import PageCache
from web_scraper.core.ratelimit import DomainRateLimiter
from web_scraper.core.urls import domain_matches, host_key, normalize_url, validate_http_url
from web_scraper.models import RawPage


def raw(fetched_at: float) -> RawPage:
    return RawPage(url="https://a.test/", final_url="https://a.test/", status_code=200, body="<p>x</p>",
                   fetched_at=fetched_at)


async def test_cache_roundtrip_and_ttl(tmp_path):
    cache = PageCache(tmp_path / "c.sqlite", ttl_seconds=60)
    await cache.put("fresh", raw(time.time()))
    await cache.put("stale", raw(time.time() - 120))

    hit = await cache.get("fresh")
    assert hit is not None and hit.from_cache is True and hit.body == "<p>x</p>"
    assert await cache.get("stale") is None
    assert await cache.get("absent") is None
    assert await cache.purge_expired() == 1
    cache.close()


async def test_cache_disabled(tmp_path):
    cache = PageCache(tmp_path / "c.sqlite", ttl_seconds=60, enabled=False)
    await cache.put("k", raw(time.time()))
    assert await cache.get("k") is None
    assert not (tmp_path / "c.sqlite").exists()


async def test_rate_limiter_spaces_requests_per_domain():
    limiter = DomainRateLimiter(default_rps=20)  # интервал 50 мс
    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire("https://www.example.com/page")
    assert time.monotonic() - started >= 0.09

    other_started = time.monotonic()
    await limiter.acquire("https://other.example/")  # другой домен не ждёт
    assert time.monotonic() - other_started < 0.03


def test_rate_limiter_overrides_match_subdomains():
    limiter = DomainRateLimiter(default_rps=1, overrides={"linkedin.com": 0.1})
    assert limiter.interval_for("https://de.linkedin.com/jobs") == 10
    assert limiter.interval_for("https://notlinkedin.com/") == 1
    assert DomainRateLimiter(default_rps=0).interval_for("x.test") == 0


def test_url_helpers():
    assert validate_http_url(" example.com/a ") == "https://example.com/a"
    assert normalize_url("HTTPS://Example.COM:443/a?b=1#frag") == "https://example.com/a?b=1"
    assert normalize_url("http://example.com") == "http://example.com/"
    assert host_key("https://www.Example.com/x") == "example.com"
    assert domain_matches("jobs.linkedin.com", "linkedin.com")
    assert not domain_matches("evil-linkedin.com", "linkedin.com")
