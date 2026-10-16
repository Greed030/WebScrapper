"""search_and_fetch: поиск + параллельная загрузка топ-N результатов."""

from __future__ import annotations

import asyncio

from ..config import Config
from ..core.http import HttpClient
from ..models import SearchAndFetchItem, SearchAndFetchRequest, SearchAndFetchResult, SearchRequest
from .fetch import Fetcher, TextBudget
from .search import search


async def search_and_fetch(
    req: SearchAndFetchRequest, *, config: Config, http: HttpClient, fetcher: Fetcher
) -> SearchAndFetchResult:
    found = await search(
        SearchRequest(query=req.query, num_results=req.n, lang=req.lang, region=req.region), config.searxng, http
    )
    hits = found.results[: req.n]
    summaries = await asyncio.gather(*(fetcher.summarize(hit.url, render=req.render) for hit in hits))

    out = config.output
    budget = TextBudget(req.max_chars_per_page or out.page_chars_in_batch, out.max_total_chars)
    items: list[SearchAndFetchItem] = []
    for rank, (hit, (summary, _)) in enumerate(zip(hits, summaries, strict=True), start=1):
        budget.take(summary)
        items.append(SearchAndFetchItem(rank=rank, title=hit.title, url=hit.url, snippet=hit.snippet, page=summary))
    return SearchAndFetchResult(query=req.query, items=items)
