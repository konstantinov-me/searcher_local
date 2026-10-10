# LocalSearch — модульный поисковик по локальным файлам

Локальный (или сетевой) поиск по содержимому файлов: имена, точные фразы,
байтовые последовательности и семантика. Архитектура описана в
[ARCHITECTURE.md](ARCHITECTURE.md); этот документ — практическое руководство:
установка, запуск, конфигурация, клиент-серверный режим, HTTPS и дельта-синхронизация.

**Ключевое свойство: Docker не обязателен.** Сервер — обычный Python-процесс
(uvicorn), Manticore Search ставится бинарным пакетом/systemd или используется
встроенный SQLite FTS5. Клиенту нужен только Python + `requests`.

---

## Содержание

1. [Возможности и уровни поиска](#1-возможности-и-уровни-поиска)
2. [Как устроено система (обзор архитектуры)](#2-архитектура-обзор)
3. [Установка](#3-установка)
4. [Быстрый старт (локальный режим)](#4-быстрый-старт-локальный-режим)
5. [Manticore Search: три способа развернуть без Docker](#5-manticore-search-без-docker)
6. [Клиент-серверный режим](#6-клиент-серверный-режим)
7. [Права доступа и области поиска](#7-права-доступа-и-области-поиска)
8. [HTTPS и реверс-прокси](#8-https-и-реверс-прокси)
9. [Инкрементальные дельта-манифесты](#9-инкрементальные-дельта-манифесты)
10. [Плагины форматов](#10-плагины-форматов)
11. [Полная таблица конфигурации](#11-конфигурация)
12. [CLI-справочник](#12-cli-справочник)
13. [REST API](#13-rest-api)
14. [Примеры использования SDK](#14-примеры-sdk)
15. [Тестирование и отладка](#15-тестирование-и-отладка)
16. [Ограничения и дорожная карта](#16-ограничения-и-дорожная-карта)
17. [Структура репозитория и веток](#17-структура-репозитория-и-веток)

---

## 1. Возможности и уровни поиска

| Уровень | Что ищет | Синтаксис запроса | Где выполняется |
|---|---|---|---|
| **L1** | Имена/пути файлов | `file:отчёт`, glob `*.pdf` | SQLite (триграммы имён) |
| **L2** | Слова, фразы, морфология, опечатки | `договор аренды`, `"точное вхождение"`, `~програмиств` | **Manticore Search** (или fallback SQLite FTS5) |
| **L3** | Последовательности байт (hex) | `bytes:DEADBEEF` | trigram-индекс + Boyer–Moore-Horspool |
| **L4** | Смысл текста, изображения «на словах» | `sem: отдых на море`, `img:cats` | FAISS/HNSW + эмбеддинги (опционально) |

Маршрутизация автоматическая (fusion): префиксы `file:` / `bytes:` / `sem:` /
`img:` выбирают searcher; всё остальное уходит в L2. Результаты разных
источников склеиваются Reciprocal Rank Fusion.

Морфология RU/EN, неточный поиск (`max_edit_distance`) и точные фразы целиком
делегированы Manticore — своего токенизатора и ранжирования в проекте нет.

Поддерживаемые форматы из коробки (как плагины): txt/md, pdf (текстовые и
сканы с опц. OCR), docx/xlsx/pptx/odt, словари ABBYY Lingvo (.dsl/.lsd),
SQLite/DBF базы данных, фото/EXIF, AI-описание картинок (опция), архивы
(zip/tar/7z/rar — содержимое индексируется рекурсивно), любой бинарный
файл — fallback по байтам.

## 2. Архитектура (обзор)

Конвейер из четырёх стадий, каждая — расширяемый модуль:

```
Scanner ──► Extractor(и) ──► Indexer(ы) ──► Searcher(ы)
 walker        text/pdf/…      manticore_text   phrases (L2)
 probe.py      office/lex…     name_index       filename (L1)
 watcher.py    archive/db…     byte_index       bytes_seq (L3)
                image_ai       vector_store     semantic (L4)
                                               fusion (роутинг+RRF)
```

* **SQLite `meta.db`** — источник истины: файлы, чанки, статусы индексации,
  триграммы имён/байтов, FTS5-fallback, состояние дельта-клиентов.
* **Manticore Search** — внешний полнотекстовый RT-индекс `docs` (L2).
* **FAISS** — векторы L4 (выключен по умолчанию).
* Тяжёлые экстракции идут в пуле процессов; обход дерева и I/O — в потоках.

Клиент-серверная надстройка (эта ветка) оборачивает тот же `App` HTTP-API с
авторизацией; локальный режим остаётся доступным без изменений.

## 3. Установка

Требуется Python ≥ 3.10.

```bash
git clone <repo-url> localsearch && cd localsearch
git checkout client-server          # ветка клиент-серверной версии (см. §17)

python3 -m venv .venv && source .venv/bin/activate
pip install -e .                    # ядро: fastapi, uvicorn, pydantic, yaml…

# опциональные наборы:
pip install -e ".[formats]"         # pdf/office/archive/image_meta
pip install -e ".[client]"          # requests (тонкому клиенту этого хватает)
pip install -e ".[semantic]"        # sentence-transformers + faiss-cpu (L4)
pip install -e ".[all]"             # всё сразу
```

Тонкому клиенту **не нужны** ни Manticore, ни тяжёлые extras:
`pip install -e ".[client]"` — и можно работать с сервером.

## 4. Быстрый старт (локальный режим)

```bash
cp config.example.yaml config.yaml   # отредактируйте roots/exclude
localsearch scan ~/Documents         # обход + индексация
localsearch query "договор аренды"   # поиск из терминала
localsearch serve                    # веб-UI на http://127.0.0.1:8080
localsearch watch ~/Documents &      # демон: live-индексация изменений
localsearch status                   # сводка по статусам
```

Без настроенного Manticore приложение автоматически деградирует в SQLite
FTS5 (чуть хуже морфология/ранжирование, но работает всё). Явный выбор
fallback: `indexer_backend: sqlite_fts`.

## 5. Manticore Search без Docker

Docker-compose вариант лежит в `deploy/docker-compose.yml` (порты проброшены
только на 127.0.0.1). Но если контейнеров нет — три альтернативы:

### 5.1. Бинарный пакет + systemd (рекомендуется для сервера)

```bash
# Debian/Ubuntu (репозиторий manticore-search):
wget https://repo.manticoresearch.com/manticore-repo_noarch.deb
sudo dpkg -i manticore-repo_noarch.deb
sudo apt update && sudo apt install manticore

sudo cp deploy/manticore/manticore.conf /etc/manticoresearch/manticore.conf
sudo cp deploy/manticore/stopwords.txt  /etc/manticoresearch/stopwords.txt
sudo systemctl enable --now manticore
mysql -h 127.0.0.1 -P 9306 -e "SHOW TABLES;"   # проверка живости
```

### 5.2. Flatpak/static binary или сборка из исходников

См. <https://manticoresearch.com/downloads/> — есть статические бинарники;
запускаются тем же `manticore.conf` (`searchd -c /path/manticore.conf`).

### 5.3. Не ставить вовсе

`indexer_backend: sqlite_fts` — L2 работает на FTS5 внутри `meta.db`.
Manticore можно подключить позже: индекс перестраивается командой
`localsearch scan --rebuild` (pending-файлы проходят конвейер заново).

DDL RT-индекса `docs` (morphology en+ru, infix, edit-distance) создаётся
автоматически при первом подключении — см. `localsearch/indexer/manticore_text.py`.

## 6. Клиент-серверный режим

Зачем: на рабочих машинах пользователей часто нельзя ставить Docker и
тяжёлые зависимости. Сервер поднимается один (Python + опционально
Manticore-бинарник), клиенты — тонкие: только Python + requests.

### 6.1. Сервер

```bash
export LOCALSEARCH_ADMIN_PASSWORD='strong-password'
localsearch serve-server --host 0.0.0.0 --port 8341
# или systemd: см. deploy/systemd/localsearch-server.service
```

При первом запуске создаётся admin (пароль из env). Сервер одновременно:
REST API + встроенный веб-UI (`http://host:8341/`) + WebSocket прогресса +
фоновая индексация (pipeline) + приём данных.

### 6.2. Тонкий клиент

```bash
localsearch client login --server https://search.corp.local --username alice
localsearch client upload ~/Docs/report.pdf --base ~/Docs
localsearch client sync-delta ~/Docs ~/Projects --with-text   # §9
localsearch client search "акт сверки" --scope own
localsearch client search "~програмиств" --scope global        # если есть права
```

### 6.3. Три режима одной кодовой базой (переключение конфигом)

| Режим | `server.url` | Что происходит |
|---|---|---|
| desktop-local | `null` | всё локально, web/api.py без auth |
| desktop-thin | `http://127.0.0.1:8341` | локальный процесс-сервер, UI как клиент |
| remote | `https://search.corp.local` | центральный сервер в сети |

SDK (`localsearch.client.SearchClient`) — единая точка входа для CLI, UI и
сторонних скриптов.

## 7. Права доступа и области поиска

Модель простая, two-tier (users + tokens в `data/users.db`):

| Роль | Может |
|---|---|
| `viewer` | только поиск |
| `indexer` | поиск + загрузка/синхронизация своих данных |
| `admin` | + создание пользователей, токенов, просмотр статуса всех namespace'ов |

| Scope пользователя | Поиск |
|---|---|
| `own` (умолч.) | только свои namespace'ы (`upload:<user>`, `remote:<user>`) |
| `global` | все данные сервера |

Запрос `GET /api/search?scope=own|global`; `scope=auto` = own для обычных,
global для глобальных. Фильтрация по `owner_id` делается **в SQL каждого
searcher'а** плюс повторная проверка на отдаче результатов
(defense-in-depth). Чужие файлы не попадают ни в сниппеты, ни в count.

## 8. HTTPS и реверс-прокси

Токены и содержимое файлов ходить по сети должны только зашифрованно.
Два варианта — выберите один.

### 8.1. Реверс-прокси nginx (рекомендуется)

Готовый конфиг: [`deploy/nginx.conf`](deploy/nginx.conf) — терминирует TLS,
корректно проксирует WebSocket `/ws/progress` и большие multipart-upload'ы
(`client_max_body_size` согласован с `server.max_upload_mb`).

Сертификат:

```bash
# Let's Encrypt (если есть публичный DNS):
sudo certbot --nginx -d search.example.local

# своя внутренняя сеть без DNS — self-signed со своим мини-CA:
sudo bash deploy/make_selfsigned_cert.sh /etc/nginx/tls search.local
# клиентам раздаём /etc/nginx/tls/ca.crt и в конфиге:
#   server: { url: https://search.local, verify_tls: /etc/localsearch/ca.crt }
```

Сам сервер при этом слушает только localhost: `serve-server --host 127.0.0.1`.

### 8.2. Прямой TLS в uvicorn (без nginx)

```bash
localsearch serve-server --host 0.0.0.0 --port 8341 \
    --tls-certfile /etc/nginx/tls/search.local.crt \
    --tls-keyfile  /etc/nginx/tls/search.local.key \
    --tls-ca-certs /etc/nginx/tls/ca.crt      # опционально: mutual-TLS
```

На стороне клиента `verify_tls`: `true` (системные CA) | путь до `ca.crt` |
`false` (только для тестов!). CLI и SDK подхватывают это из `config.yaml`.

Если в конфиге `server.url: https://...`, а сервер стартует без ключей —
`serve-server` предупредит об этом на консоли.

## 9. Инкрементальные дельта-манифесты

Проблема: `push-scan` каждый раз шлёт полный список обхода — при десятках
гигабайт манифеста и медленных каналах это расточительно, а удалённые у
клиента файлы остаются в индексе сервера «призраками».

Решение: stateful-протокол `POST /api/ingest/delta_manifest` + локальный кэш
клиента `.localsearch/<client_id>.localsearch-sync.json`
({rel_path → quick-hash}).

Алгоритм одного `sync-delta`:

1. обходим roots, считаем быстрый blake2s-хэш каждого файла (size + первые/
   последние 64 КБ);
2. сравнение с кэшем → `upsert` (новые/изменённые) и `delete` (исчезшие);
3. батчи ≤ ~8 МБ JSON; первый запрос несёт deletions и флаг `reset` (первый
   синхронизационный проход); последний — `snapshot_complete=true`;
4. сервер держит своё состояние в таблице `client_manifests`
   (user_id, client_id, rel_path, hash, file_id): upsert'ит изменившиеся,
   **удаляет то, чего нет в полном снимке**, отвечает `server_seq`;
5. клиент фиксирует новый снапшот в кэш только после успешных ответов —
   при обрыве связи следующий sync идемпотентно повторит дельту.

Итог: стабильный трафик = размер реальных изменений; rm/mv у клиента
исчезают и с сервера; `--reset` чинит любую рассинхронизацию.

```bash
localsearch client sync-delta ~/Docs --with-text          # дельта
localsearch client sync-delta ~/Docs --reset              # полная пересинхронизация
localsearch client sync-delta ~/Docs --client-id laptop-2 # несколько машин = разные client_id
```

`--with-text` отправляет извлечённый текст (локальные плагины клиента!), так
что сервер ищет по содержимому, не имея самих файлов.

## 10. Плагины форматов

Три способа добавить формат (подробно — ARCHITECTURE.md §3a):

1. **Встроенные**: text, pdf, office, lexicon, dbfiles, image_meta, image_ai,
   archive, binary_fallback — включаются списком `plugins.extractors.enable`.
2. **pip-пакет** с entry-point группы `localsearch.extractors` —
   `pip install` и формат доступен (образец: `plugins_example/`).
3. **Файл-плагин** `~/.config/localsearch/plugins/myfmt.py` с функцией
   `register_extractors(registry)` — без установки пакета вообще.

Контракт минимальный: `supports(probe) -> Support` и `extract(path, probe) ->
Artifacts(chunks, thumbnails, vectors, enqueue)`. Тяжёлые импорты — ленивые
внутри плагина; отсутствие библиотеки не ломает конвейер.

## 11. Конфигурация

Файлы читаются по порядку первого найденного: `--config PATH` →
`./config.yaml` → `~/.config/localsearch/config.yaml`. Полный образец —
[`config.example.yaml`](config.example.yaml).

| Секция/ключ | Значение | По умолчанию |
|---|---|---|
| `roots`, `exclude` | что сканировать локально | `~/`, .git/node_modules |
| `data_dir` | meta.db, uploads, векторы | `data` |
| `indexer_backend` | `manticore` \| `sqlite_fts` | `manticore` (с авто-degrade) |
| `manticore.host/port/index` | подключение к Manticore | 127.0.0.1:9306, docs |
| `vector.enabled/text_model/dim` | L4 семантика | выкл., MiniLM-L12, 384 |
| `limits.max_file_mb` | порог «полная индексация vs имя+on-disk» | 50 |
| `workers.processes/threads/queue_size` | параллелизм, backpressure | 4/8/10000 |
| `plugins.*` | enable/disable/order/overrides экстракторов | см. §10 |
| `web_host/web_port` | локальный UI | 127.0.0.1:8080 |
| `server.url/token` | режим тонкого клиента | null (локально) |
| `server.verify_tls` | true/false/путь к CA | true |
| `server.client_id/sync_state_dir` | идентификация дельта-синка | default/.localsearch |
| `server.host/port` | bind самого сервера | 127.0.0.1:8341 |
| `server.admin_user/admin_password_env` | bootstrap админа | admin/LOCALSEARCH_ADMIN_PASSWORD |
| `server.max_upload_mb/quota_mb_per_user` | лимиты приёма | 50 / 5000 |

ENV-переменные: `LOCALSEARCH_TOKEN` (клиент), `LOCALSEARCH_ADMIN_PASSWORD`
(сервер).

Встроенный веб-UI сервера (`http://host:8341/`) принимает токен в поле
подключения (он же уходит в `?token=` для WebSocket прогресса) и работает
по тем же REST-эндпоинтам §13, что и CLI/SDK.

## 12. CLI-справочник

```
localsearch scan [ROOTS...]            разовый обход + индексация
localsearch watch [ROOTS...]           демон: вотчер + конвейер
localsearch query TEXT [--level auto|name|phrase|bytes|semantic]
localsearch serve                      локальный веб-UI (без auth)
localsearch status                     счётчики статусов + ping Manticore
localsearch serve-server [--host --port]
                        [--tls-certfile --tls-keyfile --tls-ca-certs]
localsearch client login --server URL --username U
localsearch client search TEXT [--scope own|global] [--level ...]
localsearch client upload FILES... [--base DIR]
localsearch client push-scan ROOTS [--with-text]        # полный манифест
localsearch client sync-delta ROOTS [--with-text] [--reset] [--client-id ID]
localsearch client user-add NAME [--role viewer|indexer|admin] [--scope own|global]
```

## 13. REST API

Все эндпоинты, кроме `/api/hello` и `/api/login`, требуют
`Authorization: Bearer <token>`. Swagger: `/api/docs`.

| Метод/путь | Назначение | Права |
|---|---|---|
| `GET /api/hello` | инфо о сервере/ capabilities | публично |
| `POST /api/login` | username/password → token | публично |
| `GET /api/search?q=&scope=own\|global&level=&limit=&ext=` | fusion-поиск с фильтром прав | любой |
| `GET /api/file/{id}` | метаданные + превью (в пределах прав) | любой |
| `POST /api/ingest/upload` | multipart загрузка файла | indexer+ |
| `POST /api/ingest/scan_manifest` | полный манифест обхода | indexer+ |
| `POST /api/ingest/delta_manifest` | **инкрементальная дельта** (§9) | indexer+ |
| `DELETE /api/ingest/mine` | удалить все свои данные | любой |
| `POST /api/admin/users` | создать пользователя | admin |
| `POST /api/admin/tokens` | выдать долгоживущий токен | admin |
| `GET /api/admin/status` | сводка namespace'ов/пользователей | admin |
| `WS /ws/progress?token=` | события индексации в реальном времени | любой |

Ответ поиска: `{query, level, scope, count, results:[{file_id, path, name,
ext, size, mtime, namespace, score, source, snippet, span, meta}]}`.

## 14. Примеры SDK

```python
from localsearch.client import SearchClient

c = SearchClient("https://search.corp.local", verify_tls="/etc/localsearch/ca.crt")
c.login("alice", "…")                       # или SearchClient(..., token="lsk_…")

# поиск только своих / всех данных
r = c.search("технический регламент", scope="own")
r = c.search("~премьия договора", scope="global", limit=100)   # fuzzy на Manticore

# залить файл с сохранением структуры каталога
c.upload_file("/home/alice/Docs/spec.pdf", rel_path="Docs/spec.pdf")

# инкрементальная синхронизация нескольких каталогов этой машины
for resp in c.sync_delta(["/home/alice/Docs", "/home/alice/Notes"],
                         client_id="alice-desk1", with_text=True):
    print(resp["stats"])                    # {'new':..,'changed':..,'deleted':..}

# администрирование
c.create_user("bob", password="…", role="indexer", scope="own")
print(c.admin_status()["namespaces"])
```

## 15. Тестирование и отладка

```bash
pip install -e ".[dev]"
python tests/test_smoke.py        # сквозной L1/L2/L3 на sqlite_fts-бэкенде
pytest tests/                     # если добавлены pytest-тесты
```

Полезные проверки:

```bash
localsearch status                          # зависимость pipeline?
curl -s localhost:8341/api/hello            # сервер жив?
mysql -h127.0.0.1 -P9306 -e "SELECT * FROM docs LIMIT 1 OPTION ranker=sf;"
tail -f data/logs/…                          # логи конвейера (logging INFO)
sqlite3 data/meta.db "SELECT status,COUNT(*) FROM files GROUP BY status;"
sqlite3 data/meta.db "SELECT * FROM client_manifests LIMIT 20;"   # состояние синков
```

Типичные проблемы:

* **«Manticore: нет»** в `status` → сервис не запущен/неверный порт; приложение
  продолжит работу на FTS5, но без полноценной морфологии.
* **403 при search scope=global** → у пользователя scope=own (см. §7).
* **после sync-delta остались удалённые файлы** → проверьте, что дельта
  доходила полностью (последний батч `snapshot_complete=true`); при
  подозрении — `sync-delta --reset`.
* **nginx: 502 на больших upload** → `client_max_body_size` меньше файла.

## 16. Ограничения и дорожная карта

* Авторизация: bearer-токены + роли; интеграции с LDAP/AD/OIDC — в планах.
* Шифрование хранилища (at-rest) нет — сервер считается доменной зоной.
* Удалённый pull-контент (сервер сам запрашивает у клиента недостающие
  файлы) спроектирован, не реализован.
* L4 (семантика/AI картинок) требует GPU-подобных моделей — оставлен
  отдельным модулем, чтобы не тащить зависимости тонким клиентам.
* Manticore cluster/replication — on roadmap для больших установок.

## 17. Структура репозитория и веток

```
localsearch/
├── core/          контракты (Protocol'ы), модели, SQLite-слой, реестр плагинов
├── scanner/       walker (обход), probe (magic/ext/mime), watcher (watchdog)
├── extractor/    text, pdf, office, lexicon, dbfiles, image_meta, image_ai, archive, …
├── indexer/       manticore_text, sqlite_fts, name_index, byte_index, vector_store
├── searcher/      filename, phrases, bytes_seq, semantic, fusion
├── workers/       pipeline (probe→extract→index, backpressure, статусы)
├── web/           локальный API + htmx UI
├── server/        ★ HTTP-сервер: api (auth+ingest+delta), auth store, ingest service
├── client/        ★ SearchClient: поиск/upload/push-scan/sync-delta (TLS verify)
├── app.py         композиция App (graceful degrade Manticore→FTS5)
├── cli.py         typer CLI (все команды §12)
└── settings.py    pydantic-конфиг (все ключи §11)
deploy/
├── docker-compose.yml            Manticore в контейнере (опция)
├── manticore/                    manticore.conf + stopwords (бинарная установка)
├── nginx.conf                    ★ HTTPS-реверс-прокси (WebSocket-aware)
├── make_selfsigned_cert.sh       ★ свой мини-CA для внутренней сети
└── systemd/localsearch-server.service   ★ сервер как демон без Docker
plugins_example/                  образец pip-плагина формата
tests/test_smoke.py               сквозной smoke-тест
```

Ветки:

* **`main`** — локальная однопользовательская версия (ядро + web UI).
* **`client-server`** ← вы здесь: вся база main + сервер, клиент, права,
  HTTPS-обвязка, дельта-синхронизация. Ветка **самодостаточна**: серверную
  версию можно использовать и соло на одном рабочем месте
  (`desktop-thin`), ничего дополнительно ставить не нужно.
* Исторические feature-ветки (`feature/client-server`, временные qwen-*)
  служат только предками; дальнейшая работа ведётся в `client-server`,
  слияние в `main` — через PR с code review.

Лицензия — MIT (см. LICENSE).
