# Локальный поисковик по файлам — модульная архитектура

## 1. Идеология: конвейер + плагины

Система строится как **конвейер (pipeline)** из четырёх стадий, каждая из которых
расширяется независимыми модулями через единый интерфейс регистрации (plugin registry):

```
        ┌────────────┐   ┌─────────────┐   ┌──────────────┐   ┌─────────────┐
watch → │ Scanners   │ → │ Extractors  │ → │ Indexers     │ → │ Searchers   │ → Web UI
        │ (обход     │   │ (по ext /   │   │ (по типу     │   │ (по типу    │
        │  дерева)   │   │  mime-type) │   │  индекса)    │   │  запроса)   │
        └────────────┘   └─────────────┘   └──────────────┘   └─────────────┘
              file events    plain text       Manticore RT      Manticore QL
              file list      + metadata       SQLite FTS5      rank fusion
                             + embeddings     FAISS/vector     SQLite LIKE
                             + thumbnails     byte-index       fuzzy/semantic
```

Ключевое решение: **индексация и поиск разделены**. Модули-экстракторы превращают
файл в «сырьё» (текст, метаданные, эмбеддинги, байты), модули-индексеры складывают
сырьё в конкретное хранилище, модули-серчеры умеют искать в своём хранилище и
возвращают нормализованный результат. Веб-интерфейс ходит во все подключённые
серчеры одновременно и склеивает результаты (rank fusion).

Язык реализации (рекомендация): **Python 3.11+** — лучший экосистемный охват
(Manticore клиент, FAISS/sentence-transformers, EXIFToolLib, Pillow, FastAPI).
C++/Rust можно подключить позже как опциональные нативные модули через тот же ABI
реестра плагинов.

---

## 2. Структура проекта

```
localsearch/
├── pyproject.toml
├── config.yaml               # какие модули включены, пути, пороги
├── docker-compose.yml        # Manticore Search + приложение
├── localsearch/
│   ├── __main__.py           # CLI: index / search / serve / watch
│   ├── app.py                # композиция приложения (DI-контейнер)
│   ├── settings.py           # pydantic-настройки
│   ├── core/
│   │   ├── registry.py       # реестр плагинов (entry-points + runtime reg.)
│   │   ├── contracts.py      # Protocol'ы всех интерфейсов
│   │   ├── models.py         # FileRecord, Chunk, Hit, QuerySpec
│   │   ├── bus.py            # очередь событий (watch → indexer)
│   │   └── db.py             # SQLite: таблица файлов, состояния, schema_version
│   ├── scanner/
│   │   ├── walker.py         # рекурсивный обход (os.walk / scandir)
│   │   ├── watcher.py        # watchdog: инкрементальные события
│   │   ├── probe.py          # FileProbe: ext + magic-bytes + mime (для routing)
│   │   └── dedup.py          # blake3-хэш контента, пропуск дублей
│   ├── extractor/            # ← встроенные плагины по расширениям
│   │   ├── base.py           # Extractor: supports(probe)->bool, extract()->Artifacts
│   │   ├── text.py           # .txt .md .code — чистый текст (charset-detector)
│   │   ├── office.py         # .docx .xlsx .pptx .odt — python-docx/openpyxl/odfpy
│   │   ├── pdf.py            # pypdf / pdfplumber (+OCR-хук для сканов)
│   │   ├── image_meta.py     # EXIF/XMP/IPTC (piexif, exiftool)
│   │   ├── image_ai.py       # CLIP-эмбеддинги + caption (SigLIP/BLIP)
│   │   ├── audio_meta.py     # ID3/vorbis-comment
│   │   ├── lexicon.py        # Лексикон (.lex, словари Lingvo) — plg/lsd/bylx
│   │   ├── dbfiles.py        # SQLite/DBF/Journal — табличный дамп в текст
│   │   └── archive.py        # .zip/.7z/.rar — рекурсивная распаковка в tmp
│   ├── indexer/              # ← модули записи в хранилища
│   │   ├── manticore_text.py # полнотекстовой индекс (words/phrases)
│   │   ├── sqlite_fts.py     # резервный FTS5 без внешних зависимостей
│   │   ├── byte_index.py     # n-gram (trigram) индекс для подстрок байтов
│   │   ├── vector_store.py   # FAISS/HNSW по CLIP/text-эмбеддингам
│   │   └── name_index.py     # trigram-индекс имён путей в SQLite
│   ├── searcher/             # ← модули поиска (по возрастанию сложности)
│   │   ├── filename.py       # L1: имя файла
│   │   ├── phrases.py        # L2: слова/словосочетания (Manticore)
│   │   ├── bytes_seq.py      # L3: последовательность байт
│   │   ├── semantic.py       # L4: нечёткий/семантический (эмбеддинги)
│   │   └── fusion.py         # слияние результатов + RRF/rerank
│   ├── web/
│   │   ├── api.py            # FastAPI REST: /search /files /index/status
│   │   ├── ws.py             # WebSocket прогресса индексации
│   │   └── ui/               # статичный фронт (htmx или React-lite)
│   └── workers/
│       ├── pool.py           # ProcessPoolExecutor (CPU) + ThreadPool (I/O)
│       └── pipeline.py       # consumer: берёт задачу, extract→index
├── plugins_example/          # пример внешнего плагина как pip-пакет
└── tests/
```

---

## 3. Контракты (core/contracts.py)

Единые протоколы — вся модульность держится на них:

```python
class Scanner(Protocol):
    def scan(self, roots: list[Path]) -> Iterator[FileEvent]: ...

class Extractor(PluginBase, Protocol):
    name: str                          # id в config/entry-point, напр. 'pdf'
    priority: int                      # первый поддержавший файл побеждает
    formats: tuple[str, ...]           # ['application/pdf'] — для диагностики/UI
    def supports(self, probe: FileProbe) -> Support: ...  # ext/magic/mime → yes/no
    def extract(self, rec: FileRecord, ctx: ExtractCtx) -> Artifacts: ...

class Indexer(Protocol):
    target: str                        # 'manticore' | 'sqlite' | 'faiss' | ...
    consumes: set[str]                 # какие поля Artifacts нужны ('text','metadata',...)
    def index(self, rec: FileRecord, art: Artifacts) -> None: ...
    def remove(self, file_id: int) -> None: ...
    def flush(self) -> None: ...

class Searcher(Protocol):
    level: int                         # 1..4 — «сложность» из ТЗ
    capabilities: set[str]             # {'filename','phrase','bytes','semantic'}
    def search(self, q: QuerySpec) -> list[Hit]: ...
```

`Artifacts` — типизированный контейнер результата экстракции:
`text_chunks[]`, `metadata{}`, `structured_rows[]`, `embeddings[]`,
`raw_bytes_ref`, `thumbnails`, `nested[]` (вложенные файлы из архивов/контейнеров).
Индексеры берут только те поля, которые их интересуют — экстракторы ничего не
знают о хранилищах. Это позволяет менять Manticore на SQLite FTS5 (или оба сразу)
без касания экстракторов.

---

## 3a. Система плагинов обработки файлов

### 3a.1 Принципы

1. **Один контракт на все форматы.** PDF, DOCX, TXT, Лексикон, .db — всё это
   просто `Extractor`-плагины, зарегистрированные в реестре. Ядро не знает ни
   одного расширения: весь knowledge о форматах живёт в плагинах.
2. **Routing по трём сигналам** (`FileProbe`): расширение → magic-bytes (первые
   ~64 КБ) → mime (по ext). Magic решает конфликты (например `.lex` может быть
   текстовым словарём или бинарником Lingvo; переименованный ZIP под видом `.db`).
3. **Никаких тяжёлых зависимостей в ядре.** Библиотеки разбора формата
   (`pypdf`, `openpyxl`, парсер Lingvo) — зависимости самого плагина, грузятся
   лениво внутри `extract()`. Плагин без установленной зависимости помечается
   `unavailable` и молча выпадает из маршрутизации (в UI — предупреждением).
4. **Fallback-цепочка.** Если ни один специализированный плагин не поддержал
   файл, его берёт catch-all `binary_fallback` (низкий приоритет): индексация
   имени + доступность L3 байтового поиска по содержимому.

### 3a.2 Реестр и три источника плагинов

```python
# core/registry.py
class PluginRegistry:
    def discover(self) -> None:
        # 1. встроенные: pkgutil.iter_modules(localsearch.extractor)
        # 2. внешние pip-пакеты: importlib.metadata.entry_points(group="localsearch.extractors")
        # 3. drop-in директория: ~/.config/localsearch/plugins/*.py (exec_module)
        ...
    def route(self, probe: FileProbe) -> Extractor:
        cands = [p for p in self.extractors if p.supports(probe)]
        return max(cands, key=lambda p: p.priority)   # ties → объявленный порядок в config
```

Entry-point в `pyproject.toml` внешнего плагина — вся интеграция с ядром:

```toml
[project.entry-points."localsearch.extractors"]
my_lingvo = "lingvo_plugin:LingvoExtractor"

[project.entry-points."localsearch.indexers"]     # при необходимости
[project.entry-points."localsearch.searchers"]    # свои уровни/ранжирование
```

Конфиг управляет экземплярами, а не классами:

```yaml
plugins:
  extractors:
    enable:   [text, pdf, office, lexicon, dbfiles, archive, image_meta]
    disable:  [image_ai]                # дорого — включается явно
    order:    [lexicon, dbfiles, text]  # разрешение конфликтов приоритетов
    overrides:
      pdf:     {ocr: true, ocr_langs: [rus, eng]}
      dbfiles: {max_rows_per_table: 100000, mask_columns: [password, token]}
      lexicon: {extra_exts: [".dct"]}   # пользовательские расширения
```

Свойство системы: **добавление нового формата = `pip install` пакета-плагина**,
без патча ядра и без пересборки; `localsearch plugins list` покажет, что
подхватилось.

### 3a.3 Встроенные плагины формата

| Плагин | Форматы | Как извлекает | Артефакты |
|---|---|---|---|
| `text` | .txt .md .rst .csv .log, код | charset-detector (charset-normalizer), нормализация окончаний строк | text_chunks |
| `office` | .docx .xlsx .pptx .odt .ods | OOXML/ODF — zipfile + XML-парсинг .part'ов напрямую (без MS Office) | text_chunks, metadata (author/title из core.xml) |
| `pdf` | .pdf | pypdf/pdfplumber; если текст пуст → OCR-хук (опционально Tesseract/PaddleOCR) | text_chunks (span'ы с номерами страниц), metadata |
| `image_meta` | .jpg .png .tiff .heic | EXIF/XMP/IPTC (piexif/exiftool): GPS, дата, камера, теги, описание | metadata, thumbnails |
| `image_ai` | то же | CLIP/SigLIP-эмбеддинги + captioning (BLIP) | embeddings, text_chunks (caption) |
| `audio_meta` | .mp3 .flac .ogg .m4a | ID3 / vorbis-comment | metadata |
| `lexicon` | словари Лексикон/Lingvo: .dsl .lex .plg .lsd .bylx | детект по magic-заголовкам контейнеров Lingvo; парсер статей → пары «термин/перевод» | text_chunks (одна статья = один чанк), metadata (языки, имя словаря) |
| `dbfiles` | .db .sqlite .sqlite3 .dbf .mdb | SQLite — stdlib `sqlite3` read-only (`mode=ro`); DBF — парсер заголовка+записей; MDB — опционально mdbtools | structured_rows → text_chunks («таблица.колонка = значение»), metadata (схема БД) |
| `archive` | .zip .7z .rar .tar.* | потоковая распаковка во временную директорию; содержимое входит в конвейер как `nested[]` | nested-артефакты |

Два примечания к сложным случаям:

* **Лексикон.** Формат ABBYY Lingvo частично документирован: открытый `.dsl`
  (текстовый, парсится легко) обрабатываем первым, закрытые `.lsd/.bylx/
  lingvosys.res` — по magic-числам и известным структурам, инкрементально;
  каждый неподдержанный подформат не меняет контракт. Статьи словаря
  естественно ложатся в чанки: одна статья = один чанк с заголовком-термином —
  так поиск по началу слова и точным фразам в Manticore работает идеально.
* **Базы данных.** Дамп делается строго read-only (WAL не трогаем), с лимитами
  (`max_rows`, `max_total_mb`) и маскированием чувствительных колонок
  (`password`, `token` — настраивается). Schema (имена таблиц/колонок)
  индексируется отдельно в metadata — можно искать «файлы, где есть таблица
  clients». Для активно используемых live-БД предусмотрен флаг `skip_locked`.

### 3a.4 Жизненный цикл файла через плагины

```
FileProbe(ext, magic, mime)
   → registry.route() → Extractor.extract(rec, ctx)
   → Artifacts(text_chunks, metadata, structured_rows, nested, embeddings…)
   → каждый активный Indexer: if артефакты ∩ indexer.consumes → index()
   → вложенные файлы (archive.nested) рекурсивно входят в начало конвейера
     с parent_file_id — в UI как «архив/внутри/файл.pdf»
```

`ExtractCtx` даёт плагину изолированное safe-API без доступа к ядру:
`ctx.tmpdir()`, `ctx.child_probe(path)` (для архивов), `ctx.progress(p)`,
`ctx.log`, параметры из `plugins.overrides.<name>`. Ошибки плагина изолированы:
исключение → статус `error` + traceback в SQLite, конвейер продолжается
(один битый PDF не роняет индексацию).

---

## 4. Уровни поиска (по возрастанию сложности)

| Lvl | Модуль-серчер | Хранилище | Как работает |
|-----|---------------|-----------|--------------|
| 1 | `filename` | SQLite + trigram-индекс путей | LIKE/glob по имени; быстрый, всегда активен |
| 2 | `phrases` | **Manticore Search** (RT-индекс) | морфология, стоп-слова, точные фразы `"a b c"`, операторы AND/OR, BM25 |
| 3 | `bytes_seq` | собственный n-gram индекс (SQLite `bytegrams`) | запрос → hex/utf8 → trigram-фильтр кандидатов → точная проверка (Boyer–Moore) по файлу/чанку |
| 4 | `semantic` | FAISS (HNSW) + cross-encoder rerank | эмбеддинги текста (multilingual MiniLM) и картинок (CLIP/SigLIP); кросс-модальный запрос «рыжий кот на подоконнике» найдёт фото |

Особенности L3: трigram-индекс по байтам строится только для файлов под
порог размера (например ≤ 50 МБ); сверх — on-disk сканирование в пуле потоков
как деградация (медленно, но работает).

Особенности L4: текстовые эмбеддинги считаются по чанкам (~512 токенов, overlap 64),
картинки — CLIP-image-векторами; один векторный индекс с полем `modality`,
запрос роутится по типу (текст→text-tower, описание фото→image-tower).

Fusion (`fusion.py`): каждый серчер возвращает `Hit(file_id, score, span/snippet, source)`.
Слияние — Reciprocal Rank Fusion по умолчанию, опционально ML-rerank. Если включён
только L2, ответ приходит из Manticore напрямую.

---

## 5. База данных и Manticore

Два хранилища, каждое на своё:

* **SQLite** (`meta.db`) — источник истины о файлах: путь, размер, mtime,
  blake3-хэш, статус обработки (`pending/extracting/indexed/error`), метаданные,
  byte-trigrams, имена. Одна запись = один файл; связи с чанками/векторами.
* **Manticore Search** — RT-индекс `docs`: поля `title`(text), `body`(text),
  `path`(string), `mime`, `mtime`(timestamp), атрибуты для фильтрации
  (`file_id`, `size`, `date`). Морфологии: `en,ru`; charset_table для UTF-8.
  Запросы через MySQL-протокол (порт 9306) — библиотека `mysql-connector-python`
  или SQL через HTTP 9308.

docker-compose поднимает `manticoresearch/manticore` с томом `./data/manticore`.
Для «нулевых зависимостей» предусмотрен alternate-индексер `sqlite_fts` (FTS5,
поддерживает фразы в кавычках) — тот же контракт, переключение одной строкой в
config. Manticore даёт: скорость на больших объёмах, нормальное ранжирование,
инкрементальные `REPLACE INTO` без перестроения.

Конфиг индекса генерируется приложением при первом старте
(`CREATE TABLE docs (...)` через CREATE TABLE синтаксис Manticore 6.x).

---

## 6. Сканер и конвейер индексации

**Первичный обход** (`walker.py`): `os.scandir` + явный стек (не рекурсия —
без лимита глубины), параллельно по корневым директориям (threadpool, I/O-bound).
Для каждого файла — stat, magic-probe (первые 8 КБ, `python-magic`/самодельный),
blake3-хэш (опционально, для дедупликации), upsert в SQLite, событие в очередь.

**Наблюдатель** (`watcher.py`): watchdog (inotify) — события created/modified/
deleted/moved → те же очереди; debounce 2 с, пакетирование.

**Пулер** (`workers/pipeline.py`):
* очередь `queue.Queue` / при росте — disk-backed (`sqlite jobs` таблица);
* ProcessPoolExecutor для CPU-тяжёлых экстракций (PDF, AI-эмбеддинги) —
  GIL не мешает; модель worker-pool, задачи идемпотентны (key = file_id + content hash);
* ThreadPoolExecutor для лёгких (чтение текста, EXIF);
* backpressure: размер очереди ограничен, сканер притормаживает;
* ретраи и dead-letter (`error` статус + причина в БД);
* прогресс публикуется в шину → WebSocket в веб-UI.

Порядок обработки файла:
`probe → pick Extractor (priority) → Artifacts → for each enabled Indexer: index → mark indexed`.

Удаление/изменение файла: каскад `remove()` по всем индексерам по `file_id`,
затем re-enqueue (Manticore REPLACE обновляет документ атомарно).

---

## 7. Веб-интерфейс

FastAPI + минимальный SPA (htmx достаточно; опционально React):

* `GET /api/search?q=...&level=auto&filters=...` — авто-роутинг запроса:
  обычный текст → L2(+L4), префикс `hex:`/`bytes:` → L3, `name:` → L1, флаг
  `?semantic` → L4; либо fan-out во все серчеры + fusion.
* `GET /api/files/{id}` — карточка: сниппеты, метаданные, превью.
* `GET /api/thumbnail/{id}` — кэшированные превью картинок.
* `POST /api/index/rebuild`, `GET /api/index/status` (+WS) — управление индексацией.
* Фильтры: расширение, дата, размер, директория (SQL-условие поверх results).

---

## 8. Конфигурация (config.yaml)

```yaml
roots: [/home/user/docs, /mnt/photos]
exclude: ["**/.git/**", "**/node_modules/**"]
modules:
  indexers:   [manticore_text, name_index, byte_index, vector_store]
  searchers:  [filename, phrases, bytes_seq, semantic]
plugins:                       # см. подробно в §3a.2 — enable/disable/order/overrides
  extractors:
    enable:  [text, pdf, office, lexicon, dbfiles, archive, image_meta]
    disable: [image_ai]
manticore: {host: 127.0.0.1, port: 9306}
vector: {text_model: paraphrase-multilingual-MiniLM, image_model: SigLIP, dim: auto}
limits: {max_file_mb: 50, max_chunk_tokens: 512}
workers: {processes: 4, threads: 8}
```

Модуль `image_ai` тяжёлый (модель ~2 ГБ VRAM/CPU-время) — вынесен в опцию и
грузится лениво при первом использовании. Аналогично `dbfiles` для больших БД
и OCR-хук в `pdf` включаются только явным запросом пользователя.

---

## 9. SDK внешнего плагина (минимальный пример)

Внешний плагин — обычный pip-пакет, зависящий только от `localsearch-core`
(контракты + реестр, без тяжёлых зависимостей):

```python
# lingvo_plugin/__init__.py
from localsearch.core.contracts import Extractor, Artifacts, TextChunk, Support

class LingvoExtractor(Extractor):
    name = "lingvo"
    priority = 60                      # выше text(10), ниже archive(80)
    formats = ("application/x-lingvo-dsl",)

    def supports(self, probe):
        if probe.ext in (".dsl", ".lsd", ".bylx"):
            return Support.YES
        if probe.magic_startswith(b"\x4c\x69\x6e\x67"):   # "Ling"
            return Support.YES
        return Support.NO

    def extract(self, rec, ctx):
        from dsl_parser import parse_dsl   # ленивый импорт своей зависимости
        articles = parse_dsl(rec.path)
        return Artifacts(
            text_chunks=[TextChunk(text=a.body, title=a.term, ordinal=i)
                         for i, a in enumerate(articles)],
            metadata={"dict_name": articles.name, "langs": articles.langpair},
        )
```

Плюс `pyproject.toml` с entry-point (§3a.2) — и после `pip install` плагин
виден в `localsearch plugins list`, участвует в маршрутизации, получает свои
параметры из `plugins.overrides.lingvo`. Тот же механизм работает для
пользовательских drop-in скриптов в `~/.config/localsearch/plugins/` (без
упаковки в пакет).

Отдельная категория внешних плагинов — **indexer/searcher-плагины**: например,
плагин `dococr` добавляет собственный индекс OCR-текста сканов, или `rerank_bge`
подключает cross-encoder реранкер как новый searcher уровня 4+. Контракты
`Indexer/Searcher` открыты наравне с `Extractor`.

---

## 10. Дорожная карта (порядок реализации)

1. **MVP**: walker + SQLite + text-extractor + sqlite_fts + filename/phrases-серчеры + CLI `search`.
2. Manticore-индексер (замена/дополнение FTS5), docker-compose.
3. Watchdog + инкрементальная индексация + worker pool.
4. Реестр плагинов (entry-points + config enable/disable) — сразу с API,
   даже когда все плагины ещё встроенные.
5. Экстракторы: PDF, Office, архивы, метаданные изображений.
6. Нишевые экстракторы: `lexicon` (сначала .dsl), `dbfiles` (SQLite read-only).
7. Byte-seq уровень (trigram-индекс + Boyer–Moore верификация).
8. Векторный уровень: text-эмбеддинги → затем CLIP-картинки.
9. Web UI + fusion/rerank.
10. Публикация SDK-примера (`plugins_example/`), документация по написанию плагинов.

Такая последовательность даёт работающий продукт уже на шаге 1, а каждый
следующий слой добавляется без переделки предыдущих — именно за счёт
контрактов `Extractor/Indexer/Searcher` и реестра в config.
