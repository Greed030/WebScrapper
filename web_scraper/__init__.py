"""web-scraper: универсальный скрапер-инструмент для AI-агента.

Операции: search, fetch, extract, crawl, search_and_fetch. Доменной логики нет —
что искать и извлекать, решает агент.
"""

__version__ = "0.1.0"

from .config import Config, load_config  # noqa: E402
from .scraper import Scraper  # noqa: E402
from .tools import ScraperTools, tool_definitions  # noqa: E402

__all__ = ["Config", "Scraper", "ScraperTools", "load_config", "tool_definitions", "__version__"]
