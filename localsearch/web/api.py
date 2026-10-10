"""FastAPI: JSON API поиска + статический UI + WebSocket прогресса индексации.

  GET /api/search?q=&level=&limit=&ext=&path_prefix=   → fusion-результаты
  GET /api/file/{id}                                   → метаданные + превью (data: URI)
  GET /api/status                                      → счётчики, бэкенд, плагины
  POST /api/scan                                       → фоновый инкрементальный обход
  WS   /ws/progress                                    → поток событий индексации
Слушает только 127.0.0.1 — наружу не торчит.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.contracts import QuerySpec
from ..searcher.fusion import fanout

UI_DIR = Path(__file__).parent / "ui"


def create_api(a: Any) -> FastAPI:
    app = FastAPI(title="LocalSearch", docs_url="/api/docs")
    ws_clients: set[WebSocket] = set()

    # ------------------------- прогресс через WebSocket ------------------------- #

    async def broadcast(msg: dict) -> None:
        dead = []
        for ws in ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for d in dead:
            ws_clients.discard(d)

    def progress_cb(payload: dict) -> None:
        # вызывается из пула потоков конвейера → в event loop через call_soon_threadsafe
        asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

    a.pipeline.progress_cb = progress_cb

    @app.on_event("startup")
    async def _startup() -> None:
        nonlocal loop
        loop = asyncio.get_running_loop()

    loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()

    @app.websocket("/ws/progress")
    async def ws_progress(ws: WebSocket) -> None:
        await ws.accept()
        ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()          # keepalive от клиента
        except WebSocketDisconnect:
            ws_clients.discard(ws)

    # ------------------------- поиск ------------------------- #

    @app.get("/api/search")
    def api_search(q: str = Query(..., min_length=1), level: str = "auto",
                   limit: int = 50, ext: str | None = None,
                   path_prefix: str | None = None):
        spec = QuerySpec(raw=q, level=level, limit=limit,
                         filters={"ext": ext.split(",") if ext else [],
                                  "path_prefix": path_prefix})
        hits = fanout(a.searchers, spec)
        out = []
        for h in hits:
            rec = a.db.get_file(h.file_id)
            if rec is None:
                continue
            out.append({"file_id": rec.id, "path": rec.path, "name": rec.name,
                        "ext": rec.ext, "size": rec.size, "mtime": rec.mtime,
                        "score": round(h.score, 5), "source": h.source,
                        "snippet": h.snippet, "span": h.span, "meta": h.meta})
        return {"query": q, "level": level, "count": len(out), "results": out}

    # ------------------------- файлы ------------------------- #

    @app.get("/api/file/{file_id}")
    def api_file(file_id: int):
        rec = a.db.get_file(file_id)
        if rec is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        thumb = None
        rows = a.db.conn.execute(
            "SELECT body FROM chunks WHERE file_id=? AND ordinal=0", (file_id,)).fetchall()
        preview = rows[0]["body"][:800] if rows else ""
        d = dict(rec.__dict__)
        d["preview"] = preview
        d["metadata"] = rec.metadata
        if thumb:
            d["thumbnail"] = f"data:image/jpeg;base64,{base64.b64encode(thumb).decode()}"
        return d

    @app.post("/api/scan")
    def api_scan():
        from ..scanner.walker import Walker
        n = Walker(a.db, a.events, exclude=a.settings.exclude).scan(
            a.settings.roots, max_workers=a.settings.workers.threads)
        return {"enqueued": n}

    @app.get("/api/status")
    def api_status():
        return {
            "files": a.db.stats(),
            "backend": ("manticore" if a.manticore and a.manticore.connected
                        else "sqlite_fts"),
            "searchers": list(a.searchers),
            "vector_enabled": a.settings.vector.enabled,
        }

    # ------------------------- UI ------------------------- #

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")
    return app
