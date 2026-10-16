# web-scraper

[English version](README.md)

Универсальный скрапер-инструмент для AI-агента (Ezra). В нём нет доменной логики: он только ищет, открывает
страницы и извлекает данные, а **что** искать и извлекать, решает агент.

Пять операций, каждая доступна через HTTP API, tool definitions для Claude API, CLI и MCP:

| Операция | Что делает |
|---|---|
| `search` | веб-поиск через self-hosted SearXNG → `title`, `url`, `snippet` |
| `fetch` | страница → markdown основного текста, `title`, `meta`, все ссылки, e-mail, телефоны; JS-страницы автоматически открываются в Playwright |
| `extract` | структурированные данные: CSS/XPath-селекторы агента или LLM по JSON-схеме агента (ответ валидируется) |
| `crawl` | обход сайта в ширину: глубина, лимит страниц, только свой домен, фильтр ссылок регуляркой, robots.txt |
| `search_and_fetch` | поиск + загрузка топ-N результатов за один вызов |

## Установка

Нужен Python 3.11+.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

(на Linux/macOS: `source .venv/bin/activate`)

```bash
pip install -e ".[all,dev]"
```

```bash
python -m playwright install chromium
```

```bash
copy config.example.yaml config.yaml
```

Extras: `anthropic`, `openai` (SDK LLM-провайдеров), `mcp` (MCP-сервер), `all` — всё сразу, `dev` — тесты.
Ollama работает без extras.

### SearXNG

Скрапер ходит в JSON API вашего SearXNG. В его `settings.yml` должно быть:

```yaml
search:
  formats: [html, json]   # без json SearXNG отвечает 403
server:
  limiter: false          # или разрешите IP скрапера в limiter.toml, иначе будут 429
```

URL инстанса задаётся в `searxng.url`.

## Конфиг

Всё в `config.yaml` (путь можно передать `--config` или переменной `WEB_SCRAPER_CONFIG`). Полный пример
с комментариями — [config.example.yaml](config.example.yaml). Любой ключ можно опустить.

| Секция | Главное |
|---|---|
| `server` | `host`, `port` HTTP API (по умолчанию `127.0.0.1:8765`; авторизации нет — не открывайте в сеть) |
| `searxng` | `url`, язык/регион по умолчанию, сколько страниц выдачи догружать |
| `http` | таймауты, ретраи и backoff, лимит размера ответа, `proxy` (применяется и к браузеру), список User-Agent для ротации, `user_agent_overrides` — фиксированный UA для доменов (по умолчанию честный UA бота для Wikimedia, иначе они отвечают 403) |
| `browser` | Playwright-fallback: вкл/выкл, таймаут, `wait_until`, порог «пустой страницы» `min_text_chars` |
| `rate_limit` | запросов/с на домен по умолчанию и переопределения по доменам (покрывают поддомены) |
| `cache` | SQLite-кэш страниц: путь и TTL |
| `output` | обрезка текста: `max_chars` для fetch, бюджеты на страницу и на весь ответ для crawl/search_and_fetch |
| `contacts` | `phone_region` — регион для телефонов без `+кода` (например `DE`) |
| `crawl` | потолки глубины и страниц, параллельность, `respect_robots` |
| `llm` | `provider` (`anthropic` / `openai` / `ollama`), `model`, `api_key_env`, `base_url`, лимит текста для модели |
| `plugins` | включение и настройки плагинов |
| `logging` | уровень и файл логов (логи всегда идут в stderr) |

API-ключи в конфиг не пишутся: `llm.api_key_env` — это **имя** переменной окружения (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`). Для Claude подойдёт и профиль `ant auth login`. LLM-провайдер создаётся при первом
вызове `extract(mode="llm")`, так что сервер стартует и без ключа.

## Запуск

HTTP API (Swagger: http://127.0.0.1:8765/docs):

```bash
python -m web_scraper serve
```

CLI для ручной проверки (вывод — JSON-конверт; `--text` у fetch печатает только текст):

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

JSON для `--selectors` / `--schema` можно передать строкой или файлом через `@файл.json` — в PowerShell
второй вариант избавляет от проблем с кавычками. В Git Bash аргументы, начинающиеся с `/` (например
`--pattern "/blog/"`), переписываются в пути Windows — запускайте с `MSYS_NO_PATHCONV=1` или из PowerShell.
`python -m web_scraper --help` покажет все опции.

## Формат ответов

Любая операция возвращает конверт и никогда не падает исключением:

```json
{"ok": true, "result": {"url": "...", "title": "...", "text": "# ...", "truncated": true, "next_offset": 19876}}
{"ok": false, "error": {"kind": "blocked", "message": "HTTP 403: access denied by the site", "url": "...", "status_code": 403, "retryable": false}}
```

| `error.kind` | Когда |
|---|---|
| `blocked` | 401/403/429/451, страница-проверка антибота (Cloudflare, DataDome и т.п.), страница требует логина |
| `timeout` | истёк таймаут (после ретраев) |
| `not_found` | 404/410 или домен не резолвится |
| `http_error` | прочие HTTP-ошибки; `retryable: true` для 5xx |
| `network` | соединение не установлено, SearXNG недоступен |
| `unsupported_content` | PDF, картинка, архив и прочий не-HTML/не-текст |
| `invalid_input` | неверные параметры запроса (в HTTP API — статус 422 с тем же конвертом) |
| `llm_error` | ошибка LLM-провайдера или ответ не прошёл валидацию по схеме после повтора |
| `internal` | непредвиденная ошибка (подробности — в логе) |

Ошибка сайта в HTTP API приходит со статусом 200: для агента это данные, а не сбой API. В `crawl` и
`search_and_fetch` ошибка одной страницы лежит в `pages[].error` / `items[].page.error`, остальные
результаты возвращаются.

Как устроены операции:

- **fetch**: кэш → httpx (ретраи с backoff и `Retry-After`, ротация User-Agent, rate limit на домен) →
  если страница похожа на JS-оболочку или на проверку антибота, повтор в headless Chromium. Параметр
  `render` (`auto` / `always` / `never`) позволяет агенту переопределить выбор. Текст обрезается до
  `max_chars` по границе абзаца; продолжение читается вызовом с `offset = next_offset` из кэша (ссылки при
  `offset > 0` не повторяются). E-mail и телефоны ищутся по всей странице, включая футер; телефоны
  отдаются в E.164.
- **extract, mode=selectors**: `{"поле": {"css" | "xpath": "...", "attr": "text" | "html" | "<атрибут>",
  "many": bool, "fields": {...вложенные поля...}}}`. Ссылки в `href`/`src` становятся абсолютными.
  Селектор без совпадений → `null` и запись в `warnings`.
- **extract, mode=llm**: текст страницы (до `llm.max_input_chars`), список ссылок и найденные контакты
  уходят модели вместе со схемой агента и необязательным `instructions`. Claude получает схему через
  structured outputs, OpenAI работает в JSON mode, Ollama — через `format`. Ответ проверяется
  `jsonschema`; при ошибке модель получает текст ошибки и один повтор.
- **crawl**: обход в ширину; `max_depth` / `max_pages` ограничены потолками из конфига (об этом пишется
  в `warnings`), бинарные ссылки (pdf, zip, картинки) пропускаются, `stop_reason` объясняет, почему обход
  закончился (`completed`, `max_pages`, `char_budget`).

## Подключение к Ezra

### Claude API

Tool definitions генерируются из тех же Pydantic-моделей, что и API: `ScraperTools(...).definitions`,
`web_scraper.tool_definitions()`, `GET /tools` или `python -m web_scraper tools`. Имена инструментов —
`scraper_search`, `scraper_fetch`, `scraper_extract`, `scraper_crawl`, `scraper_search_and_fetch`
(префикс, чтобы не конфликтовать со встроенными серверными `web_search` / `web_fetch`).

Запустите `python -m web_scraper serve` и подключите инструменты в цикл агента:

```python
import asyncio

import anthropic
from web_scraper.tools import ScraperTools


async def main() -> None:
    client = anthropic.AsyncAnthropic()  # ключ из ANTHROPIC_API_KEY или профиля `ant auth login`
    scraper = ScraperTools("http://127.0.0.1:8765")
    messages = [{"role": "user", "content": "Найди сайт Acme GmbH в Берлине и выпиши контакты отдела кадров."}]

    while True:
        response = await client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # при отказе классификатора запрос повторяется на другой модели
            tools=scraper.definitions,  # ← подключение одной строкой
            messages=messages,
        )
        if response.stop_reason == "refusal":
            print("Модель отказалась:", response.stop_details)
            break
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            print("".join(block.text for block in response.content if block.type == "text"))
            break
        # Все вызовы одного хода выполняются параллельно и возвращаются одним сообщением.
        calls = [block for block in response.content if scraper.handles(block)]
        results = await asyncio.gather(*(scraper.tool_result(block) for block in calls))
        messages.append({"role": "user", "content": list(results)})

    await scraper.aclose()


asyncio.run(main())
```

`scraper.tool_result(block)` выполняет вызов через HTTP API и возвращает готовый блок `tool_result`
(`is_error: true`, если операция вернула `ok: false`). Если у Ezra есть свои инструменты, обрабатывайте их
рядом и отправляйте все `tool_result` одного хода в одном сообщении.

Агенты не на Python берут определения с `GET /tools` и вызывают `POST /<операция>` с `input` инструмента
в теле запроса.

### MCP

```bash
python -m web_scraper mcp
```

Сервер работает по stdio и отдаёт те же пять инструментов. Пример для конфигурации MCP-клиента
(Claude Desktop, Claude Code и т.п.):

```json
{
  "mcpServers": {
    "web-scraper": {
      "command": "C:\\path\\to\\Scrapper\\.venv\\Scripts\\python.exe",
      "args": ["-m", "web_scraper", "--config", "C:\\path\\to\\Scrapper\\config.yaml", "mcp"]
    }
  }
}
```

## Плагины

Плагин нужен, когда общему пайплайну не хватает знаний о сайте: другой эндпоинт, особая разметка,
собственные лимиты. Он лежит в `web_scraper/plugins/`, наследует `BasePlugin` и находится автоматически.

```python
# web_scraper/plugins/example_shop.py
import time

from ..core.http import raise_for_status
from ..core.parse import decode_body
from ..models import RawPage
from .base import BasePlugin


class ExampleShopPlugin(BasePlugin):
    name = "example_shop"            # ключ в секции plugins конфига
    domains = ("shop.example",)      # покрывает и поддомены

    async def fetch(self, url, *, render, use_cache):
        if "/product/" not in url:
            return None              # None → общий пайплайн
        api_url = url.replace("/product/", "/api/product/")
        response = await self.ctx.http.get(api_url)   # ретраи, UA, прокси, rate limit — как везде
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
            markdown=None,           # можно отдать готовый markdown вместо автоматического разбора
        )

    # async def extract(self, req, page): ...   # необязательно; None → общие selectors/llm по page
```

```yaml
plugins:
  example_shop:
    enabled: true
    any_setting: 42                # всё, кроме enabled, придёт в self.settings
```

Что доступно плагину: `self.ctx.http` (HTTP-клиент), `self.ctx.cache` (SQLite-кэш), `self.ctx.limiter`
(rate limiter), `self.ctx.config` (весь конфиг), `self.settings` (его секция). Из `fetch` плагин возвращает
`RawPage`: HTML (`is_html=True`) или текст, плюс необязательные готовые `markdown` и `title`. Ссылки,
e-mail, телефоны, обрезка текста и `extract` дальше работают как для любой страницы.

### LinkedIn

`plugins.linkedin.enabled: false` по умолчанию. Плагин работает только с публичными гостевыми страницами,
без логина, cookies и браузера:

| URL агента | Куда идёт запрос |
|---|---|
| `linkedin.com/jobs/view/<slug>-<id>` | `jobs-guest/jobs/api/jobPosting/<id>` → markdown: должность, компания, локация, описание, критерии |
| `linkedin.com/jobs/search?keywords=…&location=…` (`start` для пагинации, фильтры `f_*`) | `jobs-guest/jobs/api/seeMoreJobPostings/search` → список вакансий со ссылками |
| `linkedin.com/company/<name>` | публичная страница компании; редирект на authwall → `blocked` |
| всё остальное (`/in/`, `/feed`, сообщения, …) | сразу `blocked` без запроса к сайту |

Rate limit жёсткий: запросы идут строго по одному, пауза `min_interval_s` (по умолчанию 10 с + jitter,
в коде не меньше 3 с). На 429/999 плагин уходит в паузу `cooldown_on_429_s` (по умолчанию 15 минут) и всё
это время отвечает `blocked`, не обращаясь к сайту. Ответы кэшируются. Учтите, что пользовательское
соглашение LinkedIn ограничивает автоматический сбор данных — ответственность за использование на вас.

## Тесты

```bash
python -m pytest
```

Тесты работают без сети: HTTP мокается через respx, страницы берутся из `tests/fixtures/` (статья, SPA-
оболочка, контакты, листинг, страница Cloudflare, мини-сайт для crawl, ответы SearXNG, гостевые страницы
LinkedIn), браузер и LLM подменяются фейками. Интеграционный тест с настоящим Chromium:

```bash
python -m pytest -m browser
```

## Ограничения

- Никакого обхода защиты: капчи не решаются, stealth-приёмов нет; обнаруженная проверка антибота → `blocked`.
- Rate limit и пауза LinkedIn хранятся в памяти процесса: несколько воркеров uvicorn не делят лимиты
  (SQLite-кэш при этом общий).
- На Windows не запускайте `serve` через `uvicorn --reload`: в этом режиме event loop несовместим с Playwright.
