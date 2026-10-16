"""Реестр плагинов: находит подклассы BasePlugin в этой папке и включает их по конфигу."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from .base import BasePlugin, PluginContext

log = logging.getLogger(__name__)

__all__ = ["BasePlugin", "PluginContext", "PluginRegistry"]


def _discover() -> list[type[BasePlugin]]:
    for module in pkgutil.iter_modules(__path__):
        if module.name == "base":
            continue
        try:
            importlib.import_module(f"{__name__}.{module.name}")
        except Exception:
            log.exception("не удалось загрузить плагин %s", module.name)

    found: list[type[BasePlugin]] = []
    stack = list(BasePlugin.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if cls.name:
            found.append(cls)
    return found


class PluginRegistry:
    def __init__(self, plugins: list[BasePlugin] | None = None) -> None:
        self.plugins = plugins or []

    @classmethod
    def load(cls, settings: dict[str, dict[str, Any]], ctx: PluginContext) -> PluginRegistry:
        available = _discover()
        known = {plugin_cls.name for plugin_cls in available}
        for name in set(settings) - known:
            log.warning("в конфиге указан неизвестный плагин %r", name)

        plugins: list[BasePlugin] = []
        for plugin_cls in available:
            plugin_settings = dict(settings.get(plugin_cls.name) or {})
            if plugin_settings.pop("enabled", plugin_cls.enabled_by_default):
                plugins.append(plugin_cls(ctx, plugin_settings))
                log.info("плагин %s включён для %s", plugin_cls.name, ", ".join(plugin_cls.domains))
        return cls(plugins)

    @property
    def names(self) -> list[str]:
        return [plugin.name for plugin in self.plugins]

    def match(self, url: str) -> BasePlugin | None:
        return next((plugin for plugin in self.plugins if plugin.matches(url)), None)

    async def aclose(self) -> None:
        for plugin in self.plugins:
            await plugin.aclose()
