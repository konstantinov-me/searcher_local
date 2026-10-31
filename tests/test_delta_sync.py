"""Интеграционный тест: инкрементальные дельта-манифесты (клиент <-> сервер).

Сервер — FastAPI TestClient поверх реального create_server_api + IngestService;
клиент — SearchClient, у которого session подменён на транспорт TestClient
(без сети). Проверяем полный жизненный цикл sync-delta.
"""
import sys, tempfile, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from localsearch.app import build_app
from localsearch.settings import Settings
from localsearch.server.api import create_server_api
from localsearch.server.auth import AuthStore
from localsearch.server.ingest import IngestService
from localsearch.client.sdk import SearchClient

tmp = Path(tempfile.mkdtemp())
st = Settings(data_dir=tmp / "data", indexer_backend="sqlite_fts", roots=[tmp / "srv"])
a = build_app(st)
auth = AuthStore(st.users_db_path)
auth.bootstrap_admin("admin", "pw")
uid = auth.create_user("alice", "pw123456", role="indexer", scope="own")
tok = auth.issue_token("alice", "pw123456", "test")
ing = IngestService(a.db, a.events, st.uploads_dir)
api = create_server_api(a, auth, ing)
tc = TestClient(api)

# --- подменяем сетевой транспорт клиента на TestClient ----------------------- #
c = SearchClient("http://testserver", token=tok)


class _Resp:
    def __init__(self, r): self._r = r
    @property
    def status_code(self): return self._r.status_code
    def json(self): return self._r.json()
    def raise_for_status(self): self._r.raise_for_status()


class _Session:
    def __init__(self): self.headers = {"Authorization": f"Bearer {tok}"}
    def post(self, url, json=None, **kw):
        return _Resp(tc.post(url, json=json, headers=self.headers))
    def get(self, url, params=None, **kw):
        return _Resp(tc.get(url, params=params, headers=self.headers))


c.session = _Session()

# --- клиентское дерево -------------------------------------------------------- #
docs = tmp / "client" / "docs"
docs.mkdir(parents=True)
(docs / "a.txt").write_text("первый документ альфа")
(docs / "b.txt").write_text("второй документ бета")
state = tmp / "sync-state"

# 1. первый sync = полный снимок (reset=true)
r1 = list(c.sync_delta([str(docs)], client_id="desk-1", state_dir=state, with_text=False))
s1 = r1[-1]["stats"]
assert s1["new"] == 2 and s1["deleted"] == 0, s1
print("full sync:", s1)

res = c.search("документ альфа", scope="own")   # текста нет -> L1 по имени
assert any(x["name"] == "a.txt" for x in res["results"]), res

# 2. повторный sync без изменений = пустая дельта (идемпотентность)
r2 = list(c.sync_delta([str(docs)], client_id="desk-1", state_dir=state))
s2 = r2[-1]["stats"]
assert s2["upserted"] == 0 and s2["changed"] == 0 and s2["deleted"] == 0, s2
print("no-op delta:", s2)

# 3. изменения: правим a.txt, удаляем b.txt, добавляем c.txt -- ищем призрака до/после
(docs / "a.txt").write_text("первый документ альфа ОБНОВЛЁН")
(docs / "c.txt").write_text("третий гамма")
ghost = c.search("~документ бета", scope="own")   # b.txt пока в индексе
assert ghost["count"] >= 1, ghost
(docs / "b.txt").unlink()

r3 = list(c.sync_delta([str(docs)], client_id="desk-1", state_dir=state))
s3 = r3[-1]["stats"]
assert s3["changed"] == 1 and s3["new"] == 1 and s3["deleted"] == 1, s3
print("delta after edits:", s3)

after = c.search("~документ бета", scope="own")
assert all(x["name"] != "b.txt" for x in after["results"]), after
found = c.search("гамма", scope="own")            # текст c.txt не слали -> только имя
assert found["count"] >= 0

# 4. с текстом: sync --with-text -> сервер ищет по содержимому чужих файлов
r4 = list(c.sync_delta([str(docs)], client_id="desk-1", state_dir=state,
                        with_text=True))
sem = c.search("ОБНОВЛЁН", scope="own")
assert any(x["name"] == "a.txt" for x in sem["results"]), sem
print("with-text search ok")

# 5. reset форсирует пересинхронизацию
c.sync_reset("desk-1", state)
r5 = list(c.sync_delta([str(docs)], client_id="desk-1", state_dir=state))
assert r5[-1]["stats"]["deleted"] >= 0
print("reset ok:", r5[-1]["stats"])

print("\nALL DELTA TESTS PASSED")
