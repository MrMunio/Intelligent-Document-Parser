"""File format classification shared by the API (validation) and the worker (dispatch)."""
import os
import re
from typing import Optional

FORMAT_PDF = "pdf"
FORMAT_IMAGE = "image"
FORMAT_DOCX = "docx"
FORMAT_TEXT = "text"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
# Text formats we name explicitly. Any other extension is still accepted when its content is plain text.
KNOWN_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".htm", ".yaml", ".yml",
    ".log", ".ini", ".cfg", ".conf", ".rst", ".tex", ".sql", ".hl7", ".edi", ".x12", ".eml", ".srt", ".vtt",
}
# Binary office and archive formats we recognise and reject with a clear message.
UNSUPPORTED_BINARY_EXTENSIONS = {
    ".doc", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".rtf", ".zip", ".7z", ".rar", ".gz", ".tar", ".exe",
    ".dll", ".bin", ".mp3", ".mp4", ".mov", ".avi", ".gif", ".heic", ".svg", ".epub",
}

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".bmp": "image/bmp",
    ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv", ".json": "application/json",
    ".xml": "application/xml", ".html": "text/html", ".htm": "text/html", ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


def extension_of(file_name: str) -> str:
    return os.path.splitext(file_name or "")[1].lower()


def media_type_for(file_name: str) -> str:
    ext = extension_of(file_name)
    if ext in MEDIA_TYPES:
        return MEDIA_TYPES[ext]
    return "text/plain"


def safe_file_name(file_name: str, fallback: str = "document") -> str:
    """Strips directories and unusual characters. Never used to build a path from user input alone."""
    base = os.path.basename((file_name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._ ()\-]+", "_", base).strip(" .")
    return base[:180] or fallback


def looks_like_text(sample: bytes) -> bool:
    if not sample:
        return True
    if sample[:2] in (b"\xff\xfe", b"\xfe\xff") or sample[:3] == b"\xef\xbb\xbf":
        return True  # Unicode byte order mark
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 9 or (13 < b < 32 and b != 27))
    return control / len(sample) < 0.02


def classify(file_name: str, head: bytes = b"") -> Optional[str]:
    """Returns the format family for a file, or None when it is not supported."""
    ext = extension_of(file_name)
    if ext == ".pdf" or head[:5] == b"%PDF-":
        return FORMAT_PDF
    if ext == ".docx":
        return FORMAT_DOCX if (not head or head[:2] == b"PK") else None
    if ext in IMAGE_EXTENSIONS:
        return FORMAT_IMAGE
    if ext in UNSUPPORTED_BINARY_EXTENSIONS:
        return None
    if ext in KNOWN_TEXT_EXTENSIONS or looks_like_text(head):
        return FORMAT_TEXT
    return None


PHI_ACCEPT_HINT = "PDF, DOCX, images (PNG, JPG, TIFF, BMP, WEBP) and any text-based file (TXT, MD, CSV, JSON, XML, HTML, YAML, LOG)"
PARSE_ACCEPT_HINT = "PDF and images (PNG, JPG, TIFF, BMP, WEBP)"
EXTRACT_ACCEPT_HINT = "PDF, DOCX, images (PNG, JPG, TIFF, BMP, WEBP) and text-based files (TXT, MD, CSV, JSON, XML, HTML)"
