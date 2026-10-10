"""Настройки приложения (config.yaml → pydantic-модели с дефолтами)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ManticoreCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9306                      # MySQL-протокол (HTTP: 9308)
    index: str = "docs"
    realtime: bool = True                 # RT-индекс (иначе — plain + PLAIN_SOURCE)
    fallback_sqlite_fts: bool = False     # True => не использовать Manticore вовсе


class VectorCfg(BaseModel):
    enabled: bool = False                 # L4 тяжёлый — включается явно
    text_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    image_model: str = "SigLIP"           # placeholder: sentence-transformers/OpenCLIP
    dim: int = 384
    index_path: str = "data/vectors.faiss"


class LimitsCfg(BaseModel):
    max_file_mb: int = 50
    max_chunk_tokens: int = 512
    chunk_overlap: int = 64
    byte_index_max_mb: int = 50           # файлы крупнее — только on-disk скан (L3 деградация)
    head_probe_kb: int = 8


class WorkersCfg(BaseModel):
    processes: int = 4                    # CPU-тяжёлые экстракции (PDF, AI)
    threads: int = 8                      # I/O: обход дерева, чтение текста
    queue_size: int = 10_000              # backpressure


class ServerCfg(BaseModel):
    """Клиент-серверный режим.

    url=None => приложение работает полностью локально (desktop-local).
    url задан => тонкий клиент: поиск/загрузка идут на сервер по HTTP;
    токен можно получить логинами или выдать администратором.
    """
    url: str | None = None                # 'http://127.0.0.1:8341' | 'http://srv.local:8341'
    token: str | None = None              # 'lsk_...' (или из env LOCALSEARCH_TOKEN)
    username: str | None = None           # для `client login` / автологина
    verify_tls: bool | str = True         # False | путь к CA-файлу для https
    timeout: float = 30.0
    client_id: str = "default"            # имя машины/клиента для дельта-синка
    sync_state_dir: str = ".localsearch"  # где лежит кэш отправленных хэшей
    # --- параметры самого сервера (serve-server) ---
    host: str = "127.0.0.1"               # 0.0.0.0 — открыть наружу (тогда TLS/VPN!)
    port: int = 8341
    admin_user: str = "admin"             # создаётся при первом запуске
    admin_password_env: str = "LOCALSEARCH_ADMIN_PASSWORD"
    max_upload_mb: int = 50
    quota_mb_per_user: int = 5_000


class Settings(BaseModel):
    roots: list[Path] = Field(default_factory=lambda: [Path.home()])
    exclude: list[str] = Field(default_factory=lambda: ["**/.git/**", "**/node_modules/**"])
    data_dir: Path = Path("data")
    indexer_backend: str = "manticore"    # 'manticore' | 'sqlite_fts'
    searchers: list[str] = Field(
        default_factory=lambda: ["filename", "phrases", "bytes_seq"])
    manticore: ManticoreCfg = Field(default_factory=ManticoreCfg)
    vector: VectorCfg = Field(default_factory=VectorCfg)
    limits: LimitsCfg = Field(default_factory=LimitsCfg)
    workers: WorkersCfg = Field(default_factory=WorkersCfg)
    plugins: dict[str, Any] = Field(default_factory=dict)
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    server: ServerCfg = Field(default_factory=ServerCfg)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meta.db"

    @property
    def users_db_path(self) -> Path:
        return self.data_dir / "users.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        data: dict[str, Any] = {}
        for cand in [path, Path("config.yaml"), Path.home() / ".config/localsearch/config.yaml"]:
            if cand and Path(cand).is_file():
                data = yaml.safe_load(Path(cand).read_text(encoding="utf-8")) or {}
                break
        if "modules" in data:  # см. ARCHITECTURE.md §8
            mods = data.pop("modules")
            data.setdefault("searchers", mods.get("searchers"))
            ib = mods.get("indexers", [])
            data["indexer_backend"] = "sqlite_fts" if "sqlite_fts" in ib and "manticore_text" not in ib else "manticore"
            data["vector"] = {"enabled": "vector_store" in ib, **data.get("vector", {})}
        return cls(**{k: v for k, v in data.items() if k in cls.model_fields})
