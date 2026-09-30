"""Bounded text extraction for files already held in Eidolon's local workspace."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class LocalDocumentReadError(ValueError):
    pass


MAX_TEXT_CHARS = 200_000


def extract_pdf_text(source: Path | bytes, max_chars: int) -> str:
    """Share bounded PDF extraction with local files and paper retrieval."""
    try:
        reader = PdfReader(io.BytesIO(source) if isinstance(source, bytes) else source)
        parts: list[str] = []
        size = 0
        for page in reader.pages:
            part = (page.extract_text() or "").strip()
            if not part:
                continue
            remaining = max_chars - size - (2 if parts else 0)
            if remaining <= 0:
                break
            parts.append(part[:remaining])
            size += min(len(part), remaining) + (2 if len(parts) > 1 else 0)
            if size >= max_chars:
                break
        return "\n\n".join(parts)
    except (PdfReadError, ValueError, OSError):
        raise LocalDocumentReadError("The PDF could not be read as text") from None


def read_local_document(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in {".txt", ".md", ".csv", ".json"}:
            text = path.read_text(encoding="utf-8")
        elif suffix == ".pdf":
            text = extract_pdf_text(path, MAX_TEXT_CHARS)
        elif suffix in {".docx", ".xlsx", ".pptx"}:
            prefixes = {".docx": ("word/document.xml",), ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"), ".pptx": ("ppt/slides/",)}[suffix]
            with zipfile.ZipFile(path) as archive:
                parts = []
                size = 0
                count = 0
                for name in archive.namelist():
                    if not any(name.startswith(prefix) and name.endswith(".xml") for prefix in prefixes):
                        continue
                    count += 1
                    if count > 200:
                        raise LocalDocumentReadError("Document has too many readable parts")
                    if archive.getinfo(name).file_size > 4_000_000:
                        raise LocalDocumentReadError("Document part exceeds the reading limit")
                    root = ElementTree.fromstring(archive.read(name))
                    part = " ".join(node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"t", "v"} and node.text)
                    parts.append(part[: MAX_TEXT_CHARS - size])
                    size += len(parts[-1])
                    if size >= MAX_TEXT_CHARS:
                        break
                text = "\n".join(parts)
        else:
            raise LocalDocumentReadError("This file type cannot be read as text")
    except (OSError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError, ValueError) as exc:
        if isinstance(exc, LocalDocumentReadError):
            raise
        raise LocalDocumentReadError("The local document could not be read") from None
    text = re.sub(r"\x00", "", text).strip()
    if not text:
        raise LocalDocumentReadError("The document has no readable text")
    return text[:MAX_TEXT_CHARS]
