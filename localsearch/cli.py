"""CLI: localsearch [scan|watch|serve|status|query] (Typer + Rich).

  scan   — рекурсивный обход roots + индексация pending;
  watch  — скан + наблюдатель + бесконечный конвейер (демон);
  serve  — web UI/API (FastAPI, порт из конфига);
  status — счётчики по статусам файлов и доступность Manticore;
  query  — быстрый поиск из терминала (--level auto|name|phrase|bytes|semantic).
"""
from __future__ import annotations

import logging
import queue as q
import threading
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .app import build_app
from .core.contracts import QuerySpec
from .scanner.walker import Walker
from .searcher.fusion import fanout
from .settings import Settings

app = typer.Typer(help="LocalSearch — локальный поисковик по файлам", no_args_is_help=True)
console = Console()


def _settings(config: Path | None) -> Settings:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return Settings.load(config)


@app.command()
def scan(roots: list[Path] = typer.Argument(None), config: Path | None = None):
    """Разовый рекурсивный обход + индексация."""
    st = _settings(config)
    a = build_app(st)
    evq: "q.Queue" = a.events
    w = Walker(a.db, evq, exclude=st.exclude)
    n = w.scan(roots or st.roots, max_workers=st.workers.threads)
    console.print(f"[green]обход:[/green] новых/изменённых файлов: {n}")
    t0 = time.time()
    a.pipeline.drain_pending()
    console.print(f"[cyan]индексация завершена за {time.time()-t0:.1f}s[/cyan]")
    _print_status(a)


@app.command()
def watch(roots: list[Path] = typer.Argument(None), config: Path | None = None):
    """Демон: первичный скан + вотчер + бесконечная индексация."""
    st = _settings(config)
    a = build_app(st)
    from .scanner.watcher import Watcher as FsWatcher
    watcher = FsWatcher(a.db, a.events)
    rr = roots or st.roots
    Walker(a.db, a.events, exclude=st.exclude).scan(rr, st.workers.threads)
    if watcher.start(rr):
        console.print("[green]наблюдение активно[/green]")
    worker = threading.Thread(target=a.pipeline.run_forever, daemon=True)
    worker.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        a.pipeline.stop_flag.set()
        watcher.stop()


@app.command()
def query(text: str, level: str = "auto", limit: int = 20, config: Path | None = None):
    """Поиск из терминала."""
    st = _settings(config)
    a = build_app(st)
    hits = fanout(a.searchers, QuerySpec(raw=text, level=level, limit=limit))
    tbl = Table(title=f"результаты: {text!r} ({len(hits)})")
    tbl.add_column("score", justify="right", width=8)
    tbl.add_column("src", width=14)
    tbl.add_column("файл")
    tbl.add_column("сниппет")
    for h in hits[:limit]:
        rec = a.db.get_file(h.file_id)
        tbl.add_row(f"{h.score:.3f}", h.source.split(":")[-1],
                    rec.path if rec else f"id={h.file_id}", h.snippet[:80])
    console.print(tbl)


@app.command()
def serve(host: str | None = None, port: int | None = None, config: Path | None = None):
    """Web UI + JSON API (FastAPI)."""
    st = _settings(config)
    a = build_app(st)
    t = threading.Thread(target=a.pipeline.run_forever, daemon=True)
    t.start()
    Walker(a.db, a.events, exclude=st.exclude).scan(st.roots, st.workers.threads)
    from .web.api import create_api
    import uvicorn
    uvicorn.run(create_api(a), host=host or st.web_host, port=port or st.web_port)


@app.command()
def status(config: Path | None = None):
    """Сводка: сколько что проиндексировано, жив ли Manticore."""
    st = _settings(config)
    a = build_app(st)
    _print_status(a)
    if a.manticore:
        ok = a.manticore.ping()
        console.print(f"Manticore: {'[green]жив[/green]' if ok else '[red]нет[/red]'}")


def _print_status(a) -> None:
    stats = a.db.stats()
    total = sum(stats.values())
    console.print(f"файлов всего: {total}")
    for s, c in sorted(stats.items()):
        console.print(f"  {s:<10} {c:>8}")


if __name__ == "__main__":
    app()
