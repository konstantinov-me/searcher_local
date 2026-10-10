"""Аутентификация и авторизация сервера LocalSearch.

Модель прав (минималистичная, без внешних IdP — можно вырасти в OIDC позже):

  Роль        возможности
  ----------  --------------------------------------------------------------
  admin       всё: пользователи, токены, глобальный поиск, чужие данные
  indexer     загрузка своих файлов + поиск по своим (scope='own')
  viewer      только поиск; scope зависит от назначения ('own' или 'global')

Каждому клиенту выдаётся токен (случайная строка). Токен привязан к
пользователю и имеет scope:
  * scope='own'    — поиск/статус только по файлам, загруженным этим
                     пользователем (owner_id фильтруется на уровне SQL);
  * scope='global' — поиск по всем файлам сервера.

Хранение паролей/tokens — hashlib.scrypt (без зависимостей вроде bcrypt).
"""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --------------------------- хэширование паролей --------------------------- #


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = sqlite3.sqlite_version and __import__("hashlib").scrypt(
        pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        assert algo == "scrypt"
        import hashlib
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex),
                           n=2**14, r=8, p=1)
        return hmac.compare_digest(h.hex(), hash_hex)
    except Exception:
        return False


@dataclass(slots=True)
class Principal:
    """Кто сделал запрос (результат проверки токена)."""
    user_id: int
    username: str
    role: str                 # admin | indexer | viewer
    scope: str                # own | global

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_search_global(self) -> bool:
        return self.role == "admin" or (self.scope == "global")

    def owner_filter(self, requested_scope: str = "auto") -> int | None:
        """None => без фильтра по владельцу; иначе id владельца.

        Бросает PermissionError, если клиент просит global, а прав нет.
        """
        if requested_scope == "global":
            if not self.can_search_global():
                raise PermissionError("scope=global не разрешён для этой роли")
            return None
        # 'own' и 'auto' (для не-админов) => только свои файлы
        return self.user_id


class AuthStore:
    """Пользователи и токены в отдельном SQLite (users.db рядом с meta.db)."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        pw_hash TEXT,
        role TEXT NOT NULL DEFAULT 'indexer',      -- admin|indexer|viewer
        scope TEXT NOT NULL DEFAULT 'own',         -- own|global
        created REAL
    );
    CREATE TABLE IF NOT EXISTS tokens(
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        label TEXT, expires REAL, created REAL
    );
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.executescript(self._SCHEMA)

    # ------------------------- users ------------------------- #

    def create_user(self, username: str, password: str | None = None,
                    role: str = "indexer", scope: str = "own") -> int:
        if scope not in ("own", "global") or role not in ("admin", "indexer", "viewer"):
            raise ValueError("bad role/scope")
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO users(username,pw_hash,role,scope,created) VALUES(?,?,?,?,?)",
                (username, hash_password(password) if password else None,
                 role, scope, time.time()))
            return cur.lastrowid

    def get_user(self, username_or_id: str | int) -> sqlite3.Row | None:
        if isinstance(username_or_id, int) or str(username_or_id).isdigit():
            row = self.conn.execute("SELECT * FROM users WHERE id=?",
                                    (int(username_or_id),)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM users WHERE username=?",
                                    (username_or_id,)).fetchone()
        return row

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id,username,role,scope,created FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ------------------------- tokens ------------------------- #

    def issue_token(self, username: str, password: str | None = None,
                    label: str = "", ttl_days: float | None = None) -> str:
        u = self.get_user(username)
        if u is None:
            raise KeyError(f"no such user: {username}")
        if password is not None and u["pw_hash"] and not verify_password(password, u["pw_hash"]):
            raise PermissionError("bad password")
        token = "lsk_" + secrets.token_urlsafe(32)
        exp = time.time() + ttl_days * 86400 if ttl_days else None
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO tokens(token,user_id,label,expires,created) VALUES(?,?,?,?,?)",
                (token, u["id"], label, exp, time.time()))
        return token

    def revoke_token(self, token: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM tokens WHERE token=?", (token,))

    def authenticate(self, token: str) -> Principal | None:
        row = self.conn.execute(
            """SELECT t.token, t.expires, u.id uid, u.username, u.role, u.scope
               FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?""",
            (token,)).fetchone()
        if row is None:
            return None
        if row["expires"] is not None and row["expires"] < time.time():
            with self._lock, self.conn:
                self.conn.execute("DELETE FROM tokens WHERE token=?", (token,))
            return None
        return Principal(user_id=row["uid"], username=row["username"],
                         role=row["role"], scope=row["scope"])

    def bootstrap_admin(self, username: str, password: str) -> None:
        """Создать админа, если пользователей ещё нет (первый запуск сервера)."""
        n = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if n == 0:
            self.create_user(username, password, role="admin", scope="global")

    def export_summary(self) -> str:
        return json.dumps(self.list_users(), ensure_ascii=False, indent=2)
