"""FileProbe: определение типа файла для маршрутизации по экстракторам.

Порядок сигналов: расширение → magic-байты → mime (mimetypes/std).
Магические сигнатуры — без внешних зависимостей (python-magic опционален).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from ..core.contracts import FileProbe

# hex-prefix (нижний регистр, без пробелов) → canonical mime
MAGIC_TABLE: list[tuple[str, str]] = [
    ("25504446", "application/pdf"),                 # %PDF
    ("504b0304", "application/zip"),                 # PK.. (docx/xlsx/odt тоже zip!)
    ("504b0506", "application/zip"),                 # пустой zip
    ("d0cf11e0a1b11ae1", "application/msword"),      # OLE2: .doc/.xls/.mdb
    ("526172", "application/x-rar"),                 # Rar!
    ("377abcaf4392d8", "application/x-7z-compressed"),
    ("1f8b", "application/gzip"),
    ("425a68", "application/x-bzip2"),
    ("ffd8ffe0", "image/jpeg"), ("ffd8ffe1", "image/jpeg"), ("ffd8ffdb", "image/jpeg"),
    ("89504e470d0a1a0a", "image/png"),
    ("474946383961", "image/gif"), ("474946383761", "image/gif"),
    ("424d", "image/bmp"),
    ("52494646", "image/webp"),                      # RIFF....WEBP (уточняется ниже)
    ("49492a00", "image/tiff"), ("4d4d002a", "image/tiff"),   # II*. / MM*.
    ("4f67675300", "audio/ogg"),
    ("fbee", "audio/aac"), ("fff3", "audio/mpeg"), ("fffb", "audio/mpeg"),
    ("49443303", "audio/mpeg"), ("49443304", "audio/mpeg"),   # ID3 mp3
    ("524946460000000041564920", "video/avi"),
    ("3c3f786d6c", "text/xml"),                      # <?xml
    ("efbbbf", "text/plain"),                        # UTF-8 BOM
    ("feff", "text/plain"), ("fffe", "text/plain"),  # UTF-16 BOM
    ("4c0000000100000011000000", "application/x-lexicon-lsd"),  # Лексикон LSD
    ("53514c697465", "application/x-sqlite3"),       # 'SQLite format 3\0'
]

_EXT_MIME = {
    ".txt": "text/plain", ".md": "text/markdown", ".rst": "text/x-rst",
    ".log": "text/plain", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".py": "text/x-python", ".js": "text/javascript", ".html": "text/html",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".doc": "application/msword", ".xls": "application/vnd.ms-excel",
    ".rtf": "application/rtf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".heic": "image/heic",
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".wav": "audio/wav",
    ".zip": "application/zip", ".rar": "application/x-rar", ".7z": "application/x-7z-compressed",
    ".gz": "application/gzip", ".tar": "application/x-tar",
    ".sqlite": "application/x-sqlite3", ".db": "application/x-sqlite3",
    ".dbf": "application/x-dbf", ".mdb": "application/x-msaccess",
    ".lex": "application/x-lexicon", ".dsl": "application/x-lingvo-dsl",
    ".lsd": "application/x-lexicon-lsd", ".bylx": "application/x-lexicon-bylx",
}


def _sniff_mime(head: bytes, path: Path) -> str:
    h = head[:32].hex()
    for prefix, mime in MAGIC_TABLE:
        if h.startswith(prefix):
            if mime == "image/webp" and head[8:12] != b"WEBP":
                continue          # RIFF может быть wav/avi — не webp
            return mime
    # zip внутри: смотрим на расширение (docx vs plain zip)
    if h.startswith("504b") and path.suffix.lower() in _EXT_MIME \
            and _EXT_MIME[path.suffix.lower()].startswith("application/vnd"):
        return _EXT_MIME[path.suffix.lower()]
    guessed = _EXT_MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed
    # текстовый ли? первые байты валидный utf-8/latin и нет NUL
    sample = head[:2048]
    if b"\x00" not in sample:
        try:
            sample.decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return "application/octet-stream"


def probe_file(path: Path, head_kb: int = 8) -> FileProbe:
    """Читает голову файла и собирает FileProbe (ext + magic + mime + size)."""
    st = path.stat()
    with open(path, "rb") as f:
        head = f.read(head_kb * 1024)
    return FileProbe(
        path=path,
        ext=path.suffix.lower(),
        mime=_sniff_mime(head, path),
        magic=head[:16].hex(),
        head=head,
        size=st.st_size,
    )
