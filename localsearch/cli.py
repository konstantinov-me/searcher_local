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
client_app = typer.Typer(help="Клиент-серверный режим: работа с удалённым сервером",
                         no_args_is_help=True)
app.add_typer(client_app, name="client")
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


# ============================ клиент-серверный режим ============================ #

@app.command("serve-server")
def serve_server(host: str | None = None, port: int | None = None,
                 config: Path | None = None,
                 tls_certfile: Path | None = typer.Option(
                     None, help="PEM-сертификат (вместе с --tls-keyfile включает HTTPS)"),
                 tls_keyfile: Path | None = typer.Option(None, help="PEM-приватный ключ"),
                 tls_ca_certs: Path | None = typer.Option(
                     None, help="CA для mutual-TLS (необязательно)")):
    """Запустить HTTP(S)-сервер (без Docker!): uvicorn + auth + ingest + pipeline.

    Первый запуск создаёт администратора из env LOCALSEARCH_ADMIN_PASSWORD.
    Дальше: localsearch client login / user-add / upload / sync-delta / search.

    HTTPS напрямую (без реверс-прокси): --tls-certfile cert.pem --tls-keyfile key.pem
    (или server.tls_certfile/tls_keyfile в config.yaml). Для продакшн-сети
    рекомендуется nginx/caddy — см. deploy/nginx.conf.
    """
    import os
    st = _settings(config)
    a = build_app(st)
    from .server.api import create_server_api
    from .server.auth import AuthStore
    from .server.ingest import IngestService
    auth = AuthStore(st.users_db_path)
    pw = os.environ.get(st.server.admin_password_env, "")
    if not pw:
        pw = "admin"
        console.print("[yellow]LOCALSEARCH_ADMIN_PASSWORD не задан — "
                      "временный пароль админа: admin (смените!)[/yellow]")
    auth.bootstrap_admin(st.server.admin_user, pw)
    ingest = IngestService(a.db, a.events, st.uploads_dir,
                           max_file_mb=st.server.max_upload_mb,
                           max_total_mb_per_user=st.server.quota_mb_per_user)
    t = threading.Thread(target=a.pipeline.run_forever, daemon=True)
    t.start()
    Walker(a.db, a.events, exclude=st.exclude).scan(st.roots, st.workers.threads)
    import uvicorn
    ssl_kwargs = {}
    if tls_certfile and tls_keyfile:
        ssl_kwargs = {"ssl_certfile": str(tls_certfile), "ssl_keyfile": str(tls_keyfile)}
        if tls_ca_certs:
            ssl_kwargs["ssl_ca_certs"] = str(tls_ca_certs)
        console.print(f"[green]HTTPS[/green] сертификат: {tls_certfile}")
    elif str((st.server.url or "")).startswith("https://"):
        console.print("[yellow]внимание: сервер стартует по HTTP, хотя в конфиге "
                      "указан https-url клиента — передайте --tls-certfile/"
                      "--tls-keyfile или поставьте реверс-прокси (deploy/nginx.conf)"
                      "[/yellow]")
    uvicorn.run(create_server_api(a, auth, ingest),
                host=host or st.server.host, port=port or st.server.port, **ssl_kwargs)


def _client(config: Path | None) -> "tuple[Settings, object]":
    st = _settings(config)
    from .client.sdk import SearchClient
    import os
    url = st.server.url or "http://127.0.0.1:8341"
    token = st.server.token or os.environ.get("LOCALSEARCH_TOKEN")
    return st, SearchClient(url, token=token, timeout=st.server.timeout,
                            verify_tls=st.server.verify_tls)


@client_app.command("login")
def client_login(server: str | None = None, username: str = ...,
                 password: str = typer.prompt(hide_input=True)):  # type: ignore
    """Получить токен от сервера (сохранить в конфиг вручную/env)."""
    from .client.sdk import SearchClient
    c = SearchClient(server or "http://127.0.0.1:8341")
    tok = c.login(username, password)
    console.print(f"[green]token:[/green] {tok}\n"
                  f"в config.yaml:\n  server:\n    url: {c.base}\n    token: {tok}")


@client_app.command("search")
def client_search(text: str, level: str = "auto", scope: str = "auto",
                  limit: int = 20, config: Path | None = None):
    """Поиск на сервере: scope=own (свои данные) | global (все, если права)."""
    st, c = _client(config)
    res = c.search(text, level=level, scope=scope, limit=limit)
    tbl = Table(title=f"{text!r} [{res['scope']}] — {res['count']}")
    tbl.add_column("score", justify="right", width=8)
    tbl.add_column("src", width=12)
    tbl.add_column("ns", width=14)
    tbl.add_column("файл")
    for r in res["results"]:
        tbl.add_row(f"{r['score']:.4f}", r["source"].split(":")[-1],
                    r.get("namespace", ""), r["path"])
    console.print(tbl)


@client_app.command("upload")
def client_upload(paths: list[Path], base: Path | None = None,
                  config: Path | None = None):
    """Загрузить файл(ы) на сервер; base — корень для rel_path (структура сохраняется)."""
    st, c = _client(config)
    root = base or (paths[0].parent if len(paths) == 1 else Path.cwd())
    for p in paths:
        rel = str(p.resolve().relative_to(root.resolve()))
        r = c.upload_file(p, rel_path=rel)
        console.print(f"[green]ok[/green] file_id={r['file_id']} {rel}")


@client_app.command("push-scan")
def client_push_scan(roots: list[Path], with_text: bool = False,
                     config: Path | None = None):
    """Обойти локальные каталоги и отправить манифест(ы) на сервер.

    --with-text: извлечь текст локально (своими плагинами) и прислать его —
    сервер сможет искать по содержимому, не имея самих файлов.
    """
    st, c = _client(config)
    c._extract_text_on_client = with_text
    total = {}
    for resp in c.push_scan([str(r) for r in roots], with_text=with_text,
                            exclude=st.exclude):
        for k, v in resp["stats"].items():
            total[k] = total.get(k, 0) + v
    console.print(f"[cyan]манифест отправлен:[/cyan] {total}")


@client_app.command("sync-delta")
def client_sync_delta(roots: list[Path], with_text: bool = False,
                      client_id: str | None = None, reset: bool = False,
                      state_dir: Path | None = None, config: Path | None = None):
    """Инкрементальная синхронизация локальных каталогов с сервером.

    Первый запуск отправляет полный снимок; дальше — только дельту
    (изменённые/новые файлы + удалённые пути). Кэш состояния лежит в
    <state_dir>/<client_id>.localsearch-sync.json. --reset форсирует
    пересинхронизацию с нуля.
    """
    st, c = _client(config)
    cid = client_id or st.server.client_id
    sdir = state_dir or Path(st.server.sync_state_dir)
    if reset:
        c.sync_reset(cid, sdir)
    total: dict[str, int] = {}
    for resp in c.sync_delta([str(r) for r in roots], client_id=cid,
                             state_dir=sdir, with_text=with_text,
                             exclude=st.exclude):
        for k, v in resp.get("stats", {}).items():
            total[k] = total.get(k, 0) + (v if isinstance(v, int) else 0)
    console.print(f"[cyan]дельта-синк [{cid}]:[/cyan] {total}")


@client_app.command("user-add")
def client_user_add(username: str, role: str = "indexer", scope: str = "own",
                    password: str | None = typer.Option(None, hide_input=True),
                    config: Path | None = None):
    """Создать пользователя на сервере (нужен admin-токен)."""
    st, c = _client(config)
    r = c.create_user(username, password or "", role=role, scope=scope)
    console.print(f"[green]user_id={r['user_id']}[/green]")


@app.callback(invoke_without_command=True)
def _main_callback(ctx: typer.Context):
    pass


def _print_status(a) -> None:
    stats = a.db.stats()
    total = sum(stats.values())
    console.print(f"файлов всего: {total}")
    for s, c in sorted(stats.items()):
        console.print(f"  {s:<10} {c:>8}")


if __name__ == "__main__":
    app()
