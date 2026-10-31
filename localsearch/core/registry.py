"""Реестр плагинов — сердце модульности.

Три источника плагинов (в порядке приоритета переопределения):
  1. Встроенные (`localsearch.extractor.*`, `localsearch.indexer.*`, ...) — always on.
  2. Внешние pip-пакеты через entry-points групп:
        localsearch.extractors / localsearch.indexers / localsearch.searchers
  3. «Плагин-файлы» в ~/.config/localsearch/plugins/*.py — исполняются как код
     и вызывают register_<kind>() (быстрый путь без packaging).

Конфиг (`plugins:` секция config.yaml) включает/отключает и упорядочивает их.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import logging
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("localsearch.registry")

ENTRY_POINT_GROUPS = {
    "extractor": "localsearch.extractors",
    "indexer":   "localsearch.indexers",
    "searcher":  "localsearch.searchers",
}

BUILTIN_PACKAGES = {
    "extractor": "localsearch.extractor",
    "indexer":   "localsearch.indexer",
    "searcher":  "localsearch.searcher",
}


@dataclass
class Registration:
    kind: str
    plugin: Any
    source: str          # 'builtin' | 'entrypoint:<pkg>' | 'file:<path>'


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Registration]] = {
            k: {} for k in ENTRY_POINT_GROUPS}

    # ------------------------- регистрация ------------------------- #

    def register(self, kind: str, plugin: Any, source: str = "manual") -> None:
        bucket = self._items.setdefault(kind, {})
        if plugin.name in bucket:
            log.warning("plugin %s/%s переопределён (%s)", kind, plugin.name, source)
        bucket[plugin.name] = Registration(kind, plugin, source)

    def discover_builtins(self) -> None:
        """Импортирует все модули встроенных пакетов; каждый вызывает self.register."""
        for kind, pkgname in BUILTIN_PACKAGES.items():
            pkg = importlib.import_module(pkgname)
            for m in pkgutil.iter_modules(pkg.__path__):
                if m.name.startswith("_"):
                    continue
                try:
                    importlib.import_module(f"{pkgname}.{m.name}")
                except Exception as e:  # ленивая зависимость плагина не установлена
                    log.warning("builtin %s.%s пропущен: %s", pkgname, m.name, e)

    def discover_entrypoints(self) -> None:
        for kind, group in ENTRY_POINT_GROUPS.items():
            for ep in md.entry_points(group=group):
                try:
                    factory: Callable[[], Any] = ep.load()
                    self.register(kind, factory(), f"entrypoint:{ep.dist}")  # type: ignore
                except Exception as e:
                    log.warning("entry-point %s=%s не загрузился: %s", group, ep.name, e)

    def discover_dir(self, plugins_dir: Path) -> None:
        """Загружает *.py файлы из директории пользователя."""
        if not plugins_dir.is_dir():
            return
        for py in sorted(plugins_dir.glob("*.py")):
            spec = importlib.util.spec_from_file_location(f"ls_plugin_{py.stem}", py)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
                for kind in ENTRY_POINT_GROUPS:
                    fn: Callable[[Registry], None] | None = getattr(mod, f"register_{kind}s", None)
                    if fn:
                        fn(self)
                log.info("загружен плагин-файл %s", py)
            except Exception as e:
                log.error("плагин-файл %s сломался: %s", py, e)

    # ------------------------- выбор ------------------------- #

    def get(self, kind: str, name: str) -> Any | None:
        r = self._items.get(kind, {}).get(name)
        return r.plugin if r else None

    def all_of_kind(self, kind: str, cfg: "PluginsCfg | None" = None) -> list[Any]:
        items = list(self._items.get(kind, {}).values())
        if cfg:
            items = [r for r in items if cfg.enabled(kind, r.plugin.name)]
        items.sort(key=lambda r: (getattr(r.plugin, "priority", 100), r.plugin.name))
        return [r.plugin for r in items]


@dataclass
class PluginsCfg:
    """Секция `plugins:` конфигурационного файла."""
    enable: dict[str, list[str]] = field(default_factory=dict)
    disable: list[str] = field(default_factory=list)
    order: dict[str, list[str]] = field(default_factory=dict)

    def enabled(self, kind: str, name: str) -> bool:
        if name in self.disable:
            return False
        allow = self.enable.get(kind)
        return True if allow is None else name in allow

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PluginsCfg":
        ex = d.get("extractors", {})
        return cls(
            enable={k: v.get("enable", []) for k, v in d.items() if isinstance(v, dict)},
            disable=ex.get("disable", d.get("disable", [])),
            order={k: v.get("order", []) for k, v in d.items() if isinstance(v, dict)},
        )


registry = Registry()
