"""Клиент LocalSearch: HTTP-клиент к серверу + локальный сканер-донор.

Используется и CLI (`localsearch client ...`), и веб-UI, и сторонними скриптами:

    from localsearch.client import SearchClient
    c = SearchClient("http://srv:8341", token="lsk_...")
    c.search("отчёт за квартал", scope="own")        # только свои данные
    c.search("~програмиств", scope="global")         # если права позволяют
    c.upload_file("/home/u/docs/plan.pdf")           # залить файл на сервер
    c.push_scan(roots=["~/docs"], with_text=True)    # удалённый скан-манифест

Режимы развёртывания одного и того же кода (ответ на «не всегда есть Docker»):
  * desktop-local : None — приложение работает целиком локально (web/api.py);
  * desktop-thin  : SearchClient указывает на http://127.0.0.1:8341 — тот же
                    процесс-сервер без контейнеров на машине пользователя;
  * remote        : адрес центрального сервера в локальной сети.
Выбор режима — только конфигурацией (server.url / server.token).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class SearchClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0,
                 verify_tls: bool = True):
        if requests is None:
            raise RuntimeError("pip install requests (или extra 'client')")
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_tls if verify_tls is not True else True
        if isinstance(verify_tls, str):          # путь к CA-сертификату своей сети
            self.session.verify = verify_tls
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------- auth ------------------------- #

    def hello(self) -> dict:
        return self._get("/api/hello")

    def login(self, username: str, password: str) -> str:
        r = self.session.post(f"{self.base}/api/login",
                              json={"username": username, "password": password},
                              timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        self.token = data["token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        return self.token

    # ------------------------- поиск ------------------------- #

    def search(self, q: str, level: str = "auto", limit: int = 50,
               scope: str = "auto", ext: list[str] | None = None,
               path_prefix: str | None = None) -> dict:
        params: dict[str, Any] = {"q": q, "level": level, "limit": limit, "scope": scope}
        if ext:
            params["ext"] = ",".join(ext)
        if path_prefix:
            params["path_prefix"] = path_prefix
        return self._get("/api/search", params=params)

    def file_info(self, file_id: int) -> dict:
        return self._get(f"/api/file/{file_id}")

    # ------------------------- приём данных ------------------------- #

    def upload_file(self, local_path: str | Path, rel_path: str | None = None,
                    chunk_size: int = 1 << 20) -> dict:
        """Загрузить один файл (multipart, потоково)."""
        p = Path(local_path).expanduser()
        rp = rel_path or p.name
        with open(p, "rb") as f:
            r = self.session.post(
                f"{self.base}/api/ingest/upload",
                files={"file": (rp, f)},
                data={"rel_path": rp},
                timeout=max(self.timeout, 300),
            )
        r.raise_for_status()
        return r.json()

    def push_scan(self, roots: list[str], with_text: bool = False,
                  exclude: list[str] | None = None, batch_mb: int = 8) -> Iterator[dict]:
        """Обойти локальное дерево и отправить манифест(ы) на сервер.

        with_text=True — дополнительно прогнать локальные экстракторы и приложить
        извлечённый текст (тогда сервер умеет L2-поиск по содержимому, не имея
        самих файлов). Отправка батчами по ~batch_mb, чтобы не держать всё в RAM.
        """
        files = list(self._walk(roots, exclude or []))
        batch: list[dict] = []
        size = 0
        for item in files:
            batch.append(item)
            size += len(json.dumps(item))
            if with_text and item.get("chunks"):
                size += sum(len(c["text"]) for c in item["chunks"])
            if size >= batch_mb * (1 << 20) // 4:      # JSON ~ x4 от байтов utf-8
                yield self._post_manifest(batch)
                batch, size = [], 0
        if batch:
            yield self._post_manifest(batch)

    # ------------------------- инкрементальные дельта-манифесты ------------------------- #

    STATE_FILE_NAME = ".localsearch-sync.json"

    def sync_delta(self, roots: list[str], client_id: str, state_dir: str | Path | None = None,
                   with_text: bool = False, exclude: list[str] | None = None,
                   batch_mb: int = 8) -> Iterator[dict]:
        """Инкрементальная синхронизация деревьев с сервером.

        Локальный кэш состояния (state_dir/<client_id>.localsearch-sync.json)
        хранит подпись каждого отправленного файла {rel_path: hash}. При каждом
        вызове обходим roots заново и шлём на сервер ТОЛЬКО дельту:

          * upsert — новые файлы и файлы с изменившимся quick-hash;
          * delete — rel_path'ы, пропавшие с диска (rm/mv у клиента).

        Первый sync (или после sync_reset) помечает запрос reset=true — сервер
        пересобирает состояние этого client_id с нуля. Повторный sync без
        изменений отправляет пустую дельту (идемпотентность).
        """
        sdir = Path(state_dir).expanduser() if state_dir else Path.cwd() / ".localsearch"
        sdir.mkdir(parents=True, exist_ok=True)
        spath = sdir / f"{client_id}{self.STATE_FILE_NAME}"
        try:
            state = json.loads(spath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        prev: dict[str, str] = dict(state.get("files", {}))
        first_sync = "files" not in state

        self._extract_text_on_client = with_text
        current: dict[str, dict] = {}
        for item in self._walk(roots, exclude or []):
            current[item["rel_path"]] = item

        upsert = [it for rp, it in current.items() if prev.get(rp) != it.get("hash")]
        deleted = [rp for rp in prev if rp not in current]

        base = {"client_id": client_id, "snapshot_complete": True}
        if first_sync:
            base["reset"] = True

        # батчим upsert'ы; delete + reset едут в первом же запросе
        batch: list[dict] = []
        size = 0
        first = True
        n_requests = 0

        def _flush(final: bool) -> None:
            nonlocal batch, size, first, n_requests
            if not first and not final and not batch:
                return
            body = {**base, "upsert": batch, "delete": deleted if first else [],
                    "snapshot_complete": final}
            resp = self._post_delta(body)
            n_requests += 1
            yield_resp.append(resp)
            batch, size = [], 0
            first = False

        yield_resp: list[dict] = []
        for it in upsert:
            batch.append(it)
            size += len(json.dumps(it))
            if size >= batch_mb * (1 << 20) // 4:
                _flush(final=False)
        _flush(final=True)   # минимум один запрос всегда (даже пустая дельта)
        yield from yield_resp

        # фиксируем новый снапшот только после успешных ответов сервера
        state = {"client_id": client_id,
                 "server_seq": yield_resp[-1].get("_server_seq", 0),
                 "files": {rp: it.get("hash", "") for rp, it in current.items()}}
        spath.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _post_delta(self, body: dict) -> dict:
        r = self.session.post(f"{self.base}/api/ingest/delta_manifest",
                              json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
        seq = data.get("stats", {}).get("server_seq")
        if seq is not None:
            data["_server_seq"] = seq
        return data

    def sync_reset(self, client_id: str, state_dir: str | Path | None = None) -> None:
        """Удалить локальный кэш состояния — следующий sync пойдёт как полный."""
        sdir = Path(state_dir).expanduser() if state_dir else Path.cwd() / ".localsearch"
        (sdir / f"{client_id}{self.STATE_FILE_NAME}").unlink(missing_ok=True)

    def _post_manifest(self, batch: list[dict]) -> dict:
        r = self.session.post(f"{self.base}/api/ingest/scan_manifest",
                              json={"files": batch}, timeout=120)
        r.raise_for_status()
        return r.json()

    def delete_mine(self) -> dict:
        r = self.session.delete(f"{self.base}/api/ingest/mine", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ------------------------- обход дерева (для манифеста) ------------------------- #

    def _walk(self, roots: list[str], exclude: list[str]) -> Iterator[dict]:
        import fnmatch
        for root in roots:
            root_p = Path(root).expanduser()
            if not root_p.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root_p):
                dp = Path(dirpath)
                dirnames[:] = [d for d in dirnames
                               if not any(fnmatch.fnmatch(str(dp / d), pat.replace("**/", ""))
                                          or d in (".git", "node_modules", "__pycache__")
                                          for pat in exclude)]
                for fn in filenames:
                    fp = dp / fn
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    if st.st_size > 50 * (1 << 20):
                        continue
                    item: dict[str, Any] = {
                        "rel_path": str(fp.relative_to(root_p.parent)),
                        "size": st.st_size, "mtime": st.st_mtime, "ctime": st.st_ctime,
                        "ext": fp.suffix.lower(),
                        "hash": self._quick_hash(fp, st.st_size),
                    }
                    if self._extract_text_on_client:
                        chunks = self._local_extract(fp)
                        if chunks:
                            item["chunks"] = chunks
                    yield item

    _extract_text_on_client = False     # ставит True push-scan с --with-text

    @staticmethod
    def _quick_hash(p: Path, size: int) -> str:
        """blake2s от (size, первые+последние 64 КБ) — быстрая дедуп-подпись."""
        h = hashlib.blake2s(digest_size=16)
        h.update(str(size).encode())
        try:
            with open(p, "rb") as f:
                h.update(f.read(65536))
                if size > 131072:
                    f.seek(-65536, os.SEEK_END)
                    h.update(f.read(65536))
        except OSError:
            pass
        return "qs:" + h.hexdigest()

    def _local_extract(self, p: Path) -> list[dict]:
        """Локальная экстракция текста тонким клиентом (свои плагины!).

        Это ключевое преимущество клиент-серверной схемы: у клиента могут быть
        редкие форматы/словари, серверу отправляется уже нормализованный текст.
        """
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            return []
        if not text.strip():
            return []
        return [{"ordinal": 0, "title": p.name, "text": text}]

    # ------------------------- админ ------------------------- #

    def create_user(self, username: str, password: str, role: str = "indexer",
                    scope: str = "own") -> dict:
        return self._post("/api/admin/users",
                          {"username": username, "password": password,
                           "role": role, "scope": scope})

    def admin_status(self) -> dict:
        return self._get("/api/admin/status")

    # ------------------------- служебное ------------------------- #

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = self.session.post(f"{self.base}{path}", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
