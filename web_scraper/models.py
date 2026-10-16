"""Pydantic-модели входов и выходов всех операций.

Описания полей (description) уходят в tool definitions и Swagger, поэтому они на английском:
их читает LLM-агент.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from .core.urls import validate_http_url

ErrorKind = Literal[
    "blocked",
    "timeout",
    "not_found",
    "http_error",
    "network",
    "unsupported_content",
    "invalid_input",
    "llm_error",
    "internal",
]
RenderMode = Literal["auto", "always", "never"]
HttpUrlStr = Annotated[str, AfterValidator(validate_http_url)]

T = TypeVar("T")


# --------------------------------------------------------------------------- общие


class ErrorInfo(BaseModel):
    kind: ErrorKind
    message: str
    url: str | None = None
    status_code: int | None = None
    retryable: bool = False


class Envelope(BaseModel, Generic[T]):
    """Единый формат ответа любой операции."""

    ok: bool
    result: T | None = None
    error: ErrorInfo | None = None


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)


_RENDER_DESCRIPTION = (
    "'auto': plain HTTP first, headless browser only when the page looks empty or JS-rendered; "
    "'always': always render in the browser; 'never': never use the browser."
)


# --------------------------------------------------------------------------- search


class SearchRequest(_Request):
    query: str = Field(min_length=1, description="Search query.")
    num_results: int = Field(10, ge=1, le=50, description="How many results to return.")
    lang: str | None = Field(None, description="Results language, ISO 639-1 code such as 'en' or 'de'.")
    region: str | None = Field(None, description="Region, ISO 3166-1 alpha-2 code such as 'US' or 'DE'.")


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str = ""
    engine: str | None = None


class SearchResult(BaseModel):
    query: str
    results: list[SearchHit]


# --------------------------------------------------------------------------- fetch


class FetchRequest(_Request):
    url: HttpUrlStr = Field(description="Absolute http(s) URL of the page.")
    render: RenderMode = Field("auto", description=_RENDER_DESCRIPTION)
    max_chars: int | None = Field(
        None, ge=500, description="Max characters of page text to return. Server default if omitted."
    )
    offset: int = Field(
        0,
        ge=0,
        description="Character offset into the page text. Pass next_offset from a truncated response "
        "to read the next chunk (served from cache, links are not repeated).",
    )
    use_cache: bool = Field(True, description="Allow serving the page from the local cache.")


class Link(BaseModel):
    url: str
    text: str = ""
    internal: bool


class FetchResult(BaseModel):
    url: str
    final_url: str
    status_code: int
    via: str = Field(description="httpx | playwright | plugin:<name>")
    from_cache: bool
    content_type: str | None = None
    title: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)
    text: str = Field(description="Main page content as markdown (this chunk).")
    offset: int = 0
    total_chars: int
    truncated: bool
    next_offset: int | None = None
    links: list[Link] = Field(default_factory=list)
    links_total: int = 0
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- extract


class FieldSelector(BaseModel):
    """How to extract one field. Give exactly one of css / xpath."""

    model_config = ConfigDict(extra="forbid")

    css: str | None = Field(None, description="CSS selector, evaluated relative to the parent element.")
    xpath: str | None = Field(
        None,
        description="XPath relative to the parent element; may select attributes or text(), "
        "e.g. './/a/@href'.",
    )
    attr: str = Field(
        "text",
        description="What to take from matched elements: 'text' (normalized text), 'html' (outer HTML) "
        "or an attribute name such as 'href' or 'src' (URLs are made absolute).",
    )
    many: bool = Field(False, description="Return a list of all matches instead of the first one.")
    fields: dict[str, FieldSelector] | None = Field(
        None,
        description="Nested fields evaluated inside each matched element; the field then yields an "
        "object (a list of objects with many=true).",
    )

    @model_validator(mode="after")
    def _one_locator(self) -> FieldSelector:
        if (self.css is None) == (self.xpath is None):
            raise ValueError("give exactly one of 'css' or 'xpath'")
        return self


class ExtractRequest(_Request):
    url: HttpUrlStr = Field(description="Absolute http(s) URL of the page.")
    mode: Literal["selectors", "llm"] = Field(
        description="'selectors': deterministic CSS/XPath extraction; 'llm': an LLM fills a JSON Schema."
    )
    selectors: dict[str, FieldSelector] | None = Field(
        None,
        description="mode='selectors': map of output field name to selector spec. Example: "
        '{"title": {"css": "h1"}, "items": {"css": ".card", "many": true, '
        '"fields": {"name": {"css": "h3"}, "link": {"css": "a", "attr": "href"}}}}',
    )
    schema_: dict[str, Any] | None = Field(
        None,
        alias="schema",
        description="mode='llm': JSON Schema of the object to extract. Example: "
        '{"type": "object", "properties": {"company": {"type": "string"}, '
        '"emails": {"type": "array", "items": {"type": "string"}}}, "required": ["company"]}',
    )
    instructions: str | None = Field(
        None, description="mode='llm': optional extra guidance for the model (what to focus on)."
    )
    render: RenderMode = Field("auto", description=_RENDER_DESCRIPTION)
    use_cache: bool = Field(True, description="Allow serving the page from the local cache.")

    @model_validator(mode="after")
    def _check_mode(self) -> ExtractRequest:
        if self.mode == "selectors" and not self.selectors:
            raise ValueError("mode='selectors' requires a non-empty 'selectors' object")
        if self.mode == "llm" and not self.schema_:
            raise ValueError("mode='llm' requires 'schema'")
        return self


class ExtractResult(BaseModel):
    url: str
    final_url: str
    mode: str
    via: str
    from_cache: bool
    data: Any
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- crawl / search_and_fetch


class PageSummary(BaseModel):
    """Сокращённый результат fetch для пакетных операций."""

    url: str
    final_url: str | None = None
    status_code: int | None = None
    via: str | None = None
    title: str | None = None
    text: str = ""
    total_chars: int = 0
    truncated: bool = False
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None


class CrawlRequest(_Request):
    start_url: HttpUrlStr = Field(description="Page to start crawling from.")
    max_depth: int = Field(2, ge=0, description="Link depth to follow; 0 fetches only the start page.")
    max_pages: int = Field(20, ge=1, description="Maximum number of pages to fetch.")
    same_domain: bool = Field(True, description="Follow only links on the start URL's host (www. ignored).")
    url_pattern: str | None = Field(
        None,
        description="Python regex; only links whose absolute URL matches (re.search) are followed. "
        "The start URL is always fetched.",
    )
    render: RenderMode = Field("auto", description=_RENDER_DESCRIPTION)
    max_chars_per_page: int | None = Field(
        None, ge=200, description="Text characters per page. Server default if omitted."
    )

    @field_validator("url_pattern")
    @classmethod
    def _compile_pattern(cls, value: str | None) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error as e:
                raise ValueError(f"invalid regex: {e}") from e
        return value


class CrawledPage(PageSummary):
    depth: int


class CrawlResult(BaseModel):
    start_url: str
    pages: list[CrawledPage]
    pages_ok: int
    pages_failed: int
    skipped_by_robots: int = 0
    stop_reason: Literal["completed", "max_pages", "char_budget"]
    warnings: list[str] = Field(default_factory=list)


class SearchAndFetchRequest(_Request):
    query: str = Field(min_length=1, description="Search query.")
    n: int = Field(3, ge=1, le=10, description="How many top results to fetch.")
    lang: str | None = Field(None, description="Results language, ISO 639-1 code such as 'en' or 'de'.")
    region: str | None = Field(None, description="Region, ISO 3166-1 alpha-2 code such as 'US' or 'DE'.")
    render: RenderMode = Field("auto", description=_RENDER_DESCRIPTION)
    max_chars_per_page: int | None = Field(
        None, ge=200, description="Text characters per page. Server default if omitted."
    )


class SearchAndFetchItem(BaseModel):
    rank: int
    title: str
    url: str
    snippet: str = ""
    page: PageSummary


class SearchAndFetchResult(BaseModel):
    query: str
    items: list[SearchAndFetchItem]


# --------------------------------------------------------------------------- внутреннее


class RawPage(BaseModel):
    """Загруженная страница до разбора. Наружу не отдаётся, хранится в кэше."""

    url: str
    final_url: str
    status_code: int
    content_type: str | None = None
    body: str
    is_html: bool = True
    via: str = "httpx"
    from_cache: bool = False
    fetched_at: float
    markdown: str | None = Field(None, description="готовый текст от плагина вместо trafilatura")
    title: str | None = None
