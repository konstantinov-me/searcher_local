"""HTTP-сервер LocalSearch (клиент-серверная версия).

Запускается обычным uvicorn — Docker не обязателен:
    python -m localsearch.cli serve-server --host 0.0.0.0 --port 8341

Эндпоинты (все, кроме /api/hello и /api/login, требуют Bearer-токена):

  GET  /api/hello                      → публичная информация о сервере
  POST /api/login                      → username/password → token
  --- поиск (с учётом прав) ---
  GET  /api/search?q=&scope=own|global → fusion-результаты; scope=global
import dataclasses
        разрешён только ролям с правом глобального поиска
  GET  /api/file/{id}                  → метаданные файла (в пределах прав)
  --- приём данных ---
  POST /api/ingest/upload              → multipart: файл + rel_path
  POST /api/ingest/scan_manifest       → JSON-манифест удалённого скана
  DELETE /api/ingest/mine              → удалить все данные текущего пользователя
  --- админ ---
  POST /api/admin/users                → создать пользователя (admin)
  POST /api/admin/tokens               → выдать токен (admin)
  GET  /api/admin/status               → сводка по всем namespace'ам
  WS   /ws/progress                    → прогресс индексации (token в query)

Локальный режим (`localsearch serve`) использует web/api.py без авторизации —
это один и тот же App; серверная обёртка лишь добавляет auth+ingest.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import (Body, Depends, FastAPI, File, Form, Header, HTTPException,
                     Query, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse

from ..core.contracts import QuerySpec
from ..searcher.fusion import fanout
from .auth import AuthStore, Principal
from .ingest import IngestService

UI_DIR = Path(__file__).parent.parent / "web" / "ui"


def _get_principal(auth: AuthStore, authorization: str | None) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    p = auth.authenticate(authorization.split(" ", 1)[1].strip())
    if p is None:
        raise HTTPException(401, "invalid or expired token")
    return p


def create_server_api(a: Any, auth: AuthStore, ingest: IngestService) -> FastAPI:
    """a — собранный App (build_app), auth/ingest — сервисы сервера."""
    app = FastAPI(title="LocalSearch Server", docs_url="/api/docs")
    ws_clients: set[WebSocket] = set()
    loop_ref: dict[str, asyncio.AbstractEventLoop] = {}

    # ------------------------- прогресс → WebSocket ------------------------- #

    async def broadcast(msg: dict) -> None:
        dead = []
        for ws in list(ws_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for d in dead:
            ws_clients.discard(d)

    def progress_cb(payload: dict) -> None:
        loop = loop_ref.get("loop")
        if loop is not None:
            asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

    a.pipeline.progress_cb = progress_cb

    @app.on_event("startup")
    async def _startup() -> None:
        loop_ref["loop"] = asyncio.get_running_loop()

    # ------------------------- auth dependency ------------------------- #

    def current_user(authorization: str | None = Header(None)) -> Principal:
        return _get_principal(auth, authorization)

    # ------------------------- публичные ------------------------- #

    @app.get("/api/hello")
    def hello():
        return {"service": "localsearch", "version": getattr(a.settings, "version", "0.1"),
                  "capabilities": ["search", "upload", "remote_scan"],
                  "auth": "bearer-token"}

    @app.post("/api/login")
    def login(body: dict = Body(...)):
        u = auth.get_user(body.get("username", ""))
        if u is None:
            raise HTTPException(401, "bad credentials")
        from .auth import verify_password
        if not u["pw_hash"] or not verify_password(body.get("password", ""), u["pw_hash"]):
            raise HTTPException(401, "bad credentials")
        token = auth.issue_token(u["username"], label=body.get("label", "client"))
        return {"token": token, "role": u["role"], "scope": u["scope"]}

    # ------------------------- поиск ------------------------- #

    @app.get("/api/search")
    def api_search(q: str = Query(..., min_length=1), level: str = "auto",
                   limit: int = 50, ext: str | None = None,
                   path_prefix: str | None = None, scope: str = "auto",
                   user: Principal = Depends(current_user)):
        owner = user.owner_filter(scope)          # PermissionError → 403
        spec = QuerySpec(raw=q, level=level, limit=min(limit, 200),
                         filters={"ext": ext.split(",") if ext else [],
                                  "path_prefix": path_prefix,
                                  "owner": owner})
        try:
            hits = fanout(a.searchers, spec)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        out = []
        for h in hits:
            rec = a.db.get_file(h.file_id)
            if rec is None:
                continue
            # defense-in-depth: серчеры уже фильтруют по owner, но перепроверим
            if owner is not None and rec.owner_id != owner:
                continue
            out.append({"file_id": rec.id, "path": rec.path, "name": rec.name,
                        "ext": rec.ext, "size": rec.size, "mtime": rec.mtime,
                        "namespace": rec.namespace,
                        "score": round(h.score, 5), "source": h.source,
                        "snippet": h.snippet, "span": h.span, "meta": h.meta})
        return {"query": q, "level": level, "scope": "own" if owner is not None else "global",
                "count": len(out), "results": out}

    @app.get("/api/file/{file_id}")
    def api_file(file_id: int, user: Principal = Depends(current_user)):
        rec = a.db.get_file(file_id)
        if rec is None:
            raise HTTPException(404, "not found")
        if not user.can_search_global() and rec.owner_id != user.user_id:
            raise HTTPException(403, "no access to this file")
        rows = a.db.conn.execute(
            "SELECT body FROM chunks WHERE file_id=? AND ordinal=0", (file_id,)).fetchall()
        d = dataclasses.asdict(rec)
        d["preview"] = rows[0]["body"][:800] if rows else ""
        return d

    # ------------------------- приём данных ------------------------- #

    @app.post("/api/ingest/upload")
    async def upload(file: UploadFile = File(...), rel_path: str | None = Form(None),
                     user: Principal = Depends(current_user)):
        if user.role == "viewer":
            raise HTTPException(403, "role viewer cannot upload")
        rp = rel_path or file.filename or "unnamed"
        try:
            rec = ingest.save_upload(user.user_id, user.username, rp,
                                     file)          # UploadFile — итерируемый поток
        except ValueError as e:
            raise HTTPException(413 if "large" in str(e) or "quota" in str(e) else 400, str(e))
        return {"file_id": rec.id, "path": rec.path, "status": rec.status,
                "namespace": rec.namespace}

    @app.post("/api/ingest/scan_manifest")
    def scan_manifest(manifest: dict = Body(...), user: Principal = Depends(current_user)):
        if user.role == "viewer":
            raise HTTPException(403, "role viewer cannot ingest")
        stats = ingest.ingest_scan_manifest(user.user_id, user.username, manifest)
        return {"stats": stats}

    @app.post("/api/ingest/delta_manifest")
    def delta_manifest(payload: dict = Body(...), user: Principal = Depends(current_user)):
        """Инкрементальная синхронизация: дельта upsert/delete по client_id."""
        if user.role == "viewer":
            raise HTTPException(403, "role viewer cannot ingest")
        stats = ingest.ingest_delta_manifest(user.user_id, user.username, payload)
        return {"stats": stats}

    @app.delete("/api/ingest/mine")
    def delete_mine(user: Principal = Depends(current_user)):
        n = ingest.delete_namespace(user.user_id)
        return {"deleted_files": n}

    # ------------------------- админ ------------------------- #

    def require_admin(user: Principal) -> Principal:
        if not user.is_admin:
            raise HTTPException(403, "admin only")
        return user

    @app.post("/api/admin/users")
    def admin_create_user(body: dict = Body(...), user: Principal = Depends(current_user)):
        require_admin(user)
        try:
            uid = auth.create_user(body["username"], body.get("password"),
                                   body.get("role", "indexer"), body.get("scope", "own"))
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"user_id": uid}

    @app.post("/api/admin/tokens")
    def admin_issue_token(body: dict = Body(...), user: Principal = Depends(current_user)):
        require_admin(user)
        try:
            tok = auth.issue_token(body["username"], body.get("password"),
                                   body.get("label", "admin-issued"),
                                   body.get("ttl_days"))
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"token": tok}

    @app.get("/api/admin/status")
    def admin_status(user: Principal = Depends(current_user)):
        require_admin(user)
        rows = a.db.conn.execute(
            """SELECT namespace, COUNT(*) c, SUM(size) bytes,
                      SUM(status='indexed') indexed
               FROM files GROUP BY namespace""").fetchall()
        return {"namespaces": [dict(r) for r in rows], "users": auth.list_users(),
                "backend": ("manticore" if a.manticore and a.manticore.connected
                            else "sqlite_fts")}

    # ------------------------- WebSocket прогресса ------------------------- #

    @app.websocket("/ws/progress")
    async def ws_progress(ws: WebSocket, token: str = Query("")):
        p = auth.authenticate(token)
        if p is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            ws_clients.discard(ws)

    # ------------------------- встроенный веб-UI (тот же, что локальный) ------------------------- #

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    if UI_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

    return app
