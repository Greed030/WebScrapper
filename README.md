# web-scraper

[Русская версия](README.ru.md)

A general-purpose web scraping toolkit for AI agents. It contains no domain logic: it only searches the web, opens
pages and extracts data. **What** to look for and extract is decided by the agent.

Five operations, each available through an HTTP API, Claude API tool definitions, a CLI and an MCP server:

| Operation | What it does |
|---|---|
| `search` | web search through a self-hosted [SearXNG](https://github.com/searxng/searxng) → `title`, `url`, `snippet` |
| `fetch` | one page → main content as markdown, `title`, `meta`, all links, e-mail addresses, phone numbers; JavaScript-rendered pages are opened in headless Chromium automatically |
| `extract` | structured data from a page: CSS/XPath selectors supplied by the agent, or an LLM that fills a JSON Schema supplied by the agent (the answer is validated) |
| `crawl` | breadth-first site crawl: depth and page limits, same-domain mode, regex link filter, robots.txt |
| `search_and_fetch` | search plus fetching the top N results in one call |

## Installation

Python 3.11+ is required.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

(on Linux/macOS: `source .venv/bin/activate`)

```bash
pip install -e ".[all,dev]"
```

```bash
python -m playwright install chromium
```

```bash
cp config.example.yaml config.yaml
```

Extras: `anthropic`, `openai` (LLM provider SDKs), `mcp` (MCP server), `all` (everything), `dev` (tests).
Ollama needs no extra package.

### SearXNG

`search` and `search_and_fetch` call the JSON API of your SearXNG instance. The quickest way to run one is Docker:

```bash
docker run -d --name searxng -p 8080:8080 -v ./searxng:/etc/searxng searxng/searxng
```

Then enable the JSON format and disable the limiter in `./searxng/settings.yml` and restart the container:

```yaml
search:
  formats: [html, json]   # without json SearXNG answers 403
server:
  limiter: false          # or allow the scraper's IP in limiter.toml, otherwise you get 429
```

Point `searxng.url` in `config.yaml` at the instance. Public SearXNG instances almost never expose the JSON API, so
self-hosting is the practical option.

## Configuration

Everything lives in `config.yaml` (pass another path with `--config` or the `WEB_SCRAPER_CONFIG` environment
variable). [config.example.yaml](config.example.yaml) documents every key; any key can be omitted.

| Section | Highlights |
|---|---|
| `server` | `host`, `port` of the HTTP API (default `127.0.0.1:8765`; there is no authentication, do not expose it publicly) |
| `searxng` | `url`, default language/region, how many result pages to fetch |
| `http` | timeouts, retries and backoff, response size limit, `proxy` (also used by the browser), User-Agent rotation list, `user_agent_overrides` (a fixed User-Agent per domain; by default an honest bot UA for Wikimedia sites, which reject bots pretending to be browsers) |
| `browser` | Playwright fallback: on/off, timeout, `wait_until`, the "empty page" threshold `min_text_chars` |
| `rate_limit` | default requests per second per domain plus per-domain overrides (subdomains included) |
| `cache` | SQLite page cache: path and TTL |
| `output` | text limits: `max_chars` for fetch, per-page and per-response budgets for crawl / search_and_fetch |
| `contacts` | `phone_region`: region for phone numbers written without a country code (e.g. `DE`) |
| `crawl` | depth and page caps, concurrency, `respect_robots` |
| `llm` | `provider` (`anthropic` / `openai` / `ollama`), `model`, `api_key_env`, `base_url`, input size limit, `effort` (Claude) |
| `plugins` | enable and configure plugins |
| `logging` | log level and file (logs always go to stderr) |

API keys are never stored in the config: `llm.api_key_env` is the **name** of the environment variable that holds the
key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). For Claude an `ant auth login` profile works too. The LLM provider is
created on the first `extract(mode="llm")` call, so the server starts without any key.

## Running

HTTP API (Swagger UI at http://127.0.0.1:8765/docs):

```bash
python -m web_scraper serve
```

CLI for manual testing (prints the JSON envelope; `fetch --text` prints only the page text):

```bash
python -m web_scraper fetch https://example.com
```

```bash
python -m web_scraper search "python developer berlin" -n 5 --lang de --region DE
```

```bash
python -m web_scraper crawl https://example.com --depth 2 --pages 20 --pattern "/blog/"
```

```bash
python -m web_scraper search-and-fetch "acme gmbh impressum" -n 3
```

```bash
python -m web_scraper extract https://example.com --selectors @selectors.json
```

```bash
python -m web_scraper extract https://example.com --schema @schema.json --instructions "only the main office"
```

`--selectors` and `--schema` accept inline JSON or `@file.json`; the file form avoids quoting trouble in PowerShell.
In Git Bash, arguments starting with `/` (such as `--pattern "/blog/"`) are rewritten into Windows paths; prefix the
command with `MSYS_NO_PATHCONV=1` or use PowerShell. Run `python -m web_scraper --help` for all options.

## Response format

Every operation returns an envelope and never raises:

```json
{"ok": true, "result": {"url": "...", "title": "...", "text": "# ...", "truncated": true, "next_offset": 19876}}
{"ok": false, "error": {"kind": "blocked", "message": "HTTP 403: access denied by the site", "url": "...", "status_code": 403, "retryable": false}}
```

| `error.kind` | When |
|---|---|
| `blocked` | 401/403/429/451, an anti-bot challenge page (Cloudflare, DataDome, ...), a page that requires login |
| `timeout` | the request timed out (after retries) |
| `not_found` | 404/410 or the domain does not resolve |
| `http_error` | other HTTP errors; `retryable: true` for 5xx |
| `network` | connection failed, SearXNG is unreachable |
| `unsupported_content` | PDF, image, archive, file download or anything else that is not HTML/text |
| `invalid_input` | invalid request parameters (the HTTP API answers 422 with the same envelope) |
| `llm_error` | LLM provider error, missing credentials, or an answer that failed schema validation after a retry |
| `internal` | unexpected error (details in the log) |

A site error is returned by the HTTP API with status 200: for the agent it is data, not an API failure. In `crawl`
and `search_and_fetch` a failed page is reported in `pages[].error` / `items[].page.error` and the other results are
still returned.

How the operations work:

- **fetch**: cache → httpx (retries with exponential backoff and `Retry-After`, User-Agent rotation, per-domain rate
  limit) → if the page looks like a JavaScript shell or an anti-bot challenge, it is retried in headless Chromium.
  The `render` parameter (`auto` / `always` / `never`) lets the agent override that choice. Text is cut to
  `max_chars` on a paragraph boundary; read the next chunk with `offset = next_offset` (served from the cache, links
  are not repeated when `offset > 0`). E-mail addresses and phone numbers are collected from the whole page, footer
  included; phones are returned in E.164.
- **extract, mode=selectors**: `{"field": {"css" | "xpath": "...", "attr": "text" | "html" | "<attribute>",
  "many": bool, "fields": {...nested fields...}}}`. URLs in `href`/`src` are made absolute. A selector that matches
  nothing yields `null` and an entry in `warnings`.
- **extract, mode=llm**: the page text (up to `llm.max_input_chars`), its links and detected contacts are sent to the
  model together with the agent's schema and optional `instructions`. Claude receives the schema through structured
  outputs, OpenAI runs in JSON mode, Ollama gets it as `format`. The answer is checked with `jsonschema`; on failure
  the model gets the validation error and one retry.
- **crawl**: breadth-first; `max_depth` / `max_pages` are capped by the config (reported in `warnings`), links to
  binary files (pdf, zip, images) are skipped, `stop_reason` tells why the crawl ended (`completed`, `max_pages`,
  `char_budget`).

## Connecting an agent

### Claude API

Tool definitions are generated from the same Pydantic models as the API: `ScraperTools(...).definitions`,
`web_scraper.tool_definitions()`, `GET /tools`, or `python -m web_scraper tools`. Tool names are `scraper_search`,
`scraper_fetch`, `scraper_extract`, `scraper_crawl`, `scraper_search_and_fetch` (prefixed so they do not clash with
Anthropic's server-side `web_search` / `web_fetch` tools).

Start `python -m web_scraper serve` and plug the tools into the agent loop:

```python
import asyncio

import anthropic
from web_scraper.tools import ScraperTools


async def main() -> None:
    client = anthropic.AsyncAnthropic()  # uses ANTHROPIC_API_KEY or an `ant auth login` profile
    scraper = ScraperTools("http://127.0.0.1:8765")
    messages = [{"role": "user", "content": "Find the website of Acme GmbH in Berlin and list its HR contacts."}]

    while True:
        response = await client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if a safety classifier declines, the request is retried on another model
            tools=scraper.definitions,  # ← the whole integration
            messages=messages,
        )
        if response.stop_reason == "refusal":
            print("The model declined:", response.stop_details)
            break
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            print("".join(block.text for block in response.content if block.type == "text"))
            break
        # Run all tool calls of the turn in parallel and return them in one message.
        calls = [block for block in response.content if scraper.handles(block)]
        results = await asyncio.gather(*(scraper.tool_result(block) for block in calls))
        messages.append({"role": "user", "content": list(results)})

    await scraper.aclose()


asyncio.run(main())
```

`scraper.tool_result(block)` executes the call through the HTTP API and returns a ready `tool_result` block
(`is_error: true` when the operation returned `ok: false`). If your agent has other tools, handle them alongside and
send all `tool_result` blocks of a turn in a single message.

Agents written in other languages can fetch the definitions from `GET /tools` and call `POST /<operation>` with the
tool `input` as the request body.

### n8n and other workflow tools

Use an **HTTP Request** node: `POST http://127.0.0.1:8765/search_and_fetch` (or `/fetch`, `/extract`, ...) with the
same JSON body the tool takes, e.g. `{"query": "python developer berlin site:jobs.lever.co", "n": 5}`. Scheduling,
loops over links and storage are done with regular n8n nodes. If n8n runs in Docker, it reaches the host at
`host.docker.internal`; the scraper must then listen on `0.0.0.0`, so keep the port closed to the outside world.

### MCP

```bash
python -m web_scraper mcp
```

The server speaks MCP over stdio and exposes the same five tools. Example client configuration (Claude Desktop,
Claude Code, etc.):

```json
{
  "mcpServers": {
    "web-scraper": {
      "command": "C:\\path\\to\\WebScrapper\\.venv\\Scripts\\python.exe",
      "args": ["-m", "web_scraper", "--config", "C:\\path\\to\\WebScrapper\\config.yaml", "mcp"]
    }
  }
}
```

## Plugins

A plugin is for sites where the generic pipeline is not enough: a different endpoint, special markup, stricter
limits. Plugins live in `web_scraper/plugins/`, subclass `BasePlugin` and are discovered automatically.

```python
# web_scraper/plugins/example_shop.py
import time

from ..core.http import raise_for_status
from ..core.parse import decode_body
from ..models import RawPage
from .base import BasePlugin


class ExampleShopPlugin(BasePlugin):
    name = "example_shop"            # key in the plugins section of the config
    domains = ("shop.example",)      # subdomains included

    async def fetch(self, url, *, render, use_cache):
        if "/product/" not in url:
            return None              # None → generic pipeline
        api_url = url.replace("/product/", "/api/product/")
        response = await self.ctx.http.get(api_url)   # retries, User-Agent, proxy, rate limit as usual
        raise_for_status(response.status_code, url)
        body = decode_body(response.content, response.content_type)
        return RawPage(
            url=url,
            final_url=api_url,
            status_code=response.status_code,
            content_type=response.content_type,
            body=body,
            is_html=False,
            via=f"plugin:{self.name}",
            fetched_at=time.time(),
            markdown=None,           # optionally return ready markdown instead of automatic parsing
        )

    # async def extract(self, req, page): ...   # optional; None → generic selectors/llm on `page`
```

```yaml
plugins:
  example_shop:
    enabled: true
    any_setting: 42                # everything except `enabled` ends up in self.settings
```

A plugin has access to `self.ctx.http` (HTTP client), `self.ctx.cache` (SQLite cache), `self.ctx.limiter` (rate
limiter), `self.ctx.config` (the whole config) and `self.settings` (its own section). `fetch` returns a `RawPage`:
HTML (`is_html=True`) or plain text, plus optional ready-made `markdown` and `title`. Links, contacts, text
truncation and `extract` then work as for any other page.

### LinkedIn

Disabled by default (`plugins.linkedin.enabled: false`). The plugin uses only public guest pages, without login,
cookies or a browser:

| URL passed by the agent | Request that is actually made |
|---|---|
| `linkedin.com/jobs/view/<slug>-<id>` | `jobs-guest/jobs/api/jobPosting/<id>` → markdown: title, company, location, description, job criteria |
| `linkedin.com/jobs/search?keywords=…&location=…` (`start` for paging, `f_*` filters) | `jobs-guest/jobs/api/seeMoreJobPostings/search` → list of postings with links |
| `linkedin.com/company/<name>` | the public company page; a redirect to the auth wall → `blocked` |
| anything else (`/in/`, `/feed`, messages, …) | `blocked` immediately, without contacting the site |

The rate limit is strict: requests go one at a time with a `min_interval_s` pause (10 s + jitter by default, never
less than 3 s in code). On HTTP 429/999 the plugin enters a `cooldown_on_429_s` pause (15 minutes by default) and
answers `blocked` without touching the site until it ends. Responses are cached. Note that LinkedIn's user agreement
restricts automated data collection; you are responsible for how you use it.

## Tests

```bash
python -m pytest
```

The suite runs offline: HTTP is mocked with respx, pages come from `tests/fixtures/` (an article, a SPA shell, a
contacts page, a product listing, a Cloudflare challenge, a mini site for crawling, SearXNG responses, LinkedIn guest
pages), the browser and the LLM are replaced with fakes. Integration test with a real Chromium:

```bash
python -m pytest -m browser
```

## Limitations

- No protection bypassing: CAPTCHAs are not solved and there are no stealth tricks; a detected anti-bot challenge is
  reported as `blocked`. Heavily protected sites (Indeed, for example) will usually return `blocked`.
- The rate limiter and the LinkedIn cooldown live in process memory: several uvicorn workers do not share them
  (the SQLite cache is shared).
- On Windows do not run the API through `uvicorn --reload`: that mode uses an event loop that Playwright does not
  support.
