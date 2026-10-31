from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .models import Chunk, Document

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".html", ".htm", ".jsonl"}


def _digest(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _clean_html(raw: str) -> str:
    parser = _HTMLText()
    parser.feed(raw)
    return "\n".join(parser.parts)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text)
    return text.strip()


def _document_from_text(source: str, title: str, text: str, url: str = "") -> Document:
    normalized = _normalize_text(text)
    if not normalized:
        raise ValueError(f"Document has no text: {source}")
    identity = f"{source}\0{normalized}"
    return Document(
        doc_id=f"DOC-{_digest(identity, 16)}",
        source=source,
        title=(title or Path(source).stem).strip(),
        text=normalized,
        url=url.strip(),
    )


def _read_jsonl(path: Path, source: str) -> Iterable[Document]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {source}:{line_number}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"Expected object at {source}:{line_number}")
            text = item.get("text", item.get("contents", item.get("content")))
            if not isinstance(text, str):
                raise ValueError(f"Missing text/contents/content at {source}:{line_number}")
            title = str(item.get("title", item.get("id", f"{Path(source).stem}:{line_number}")))
            row_source = str(item.get("source", f"{source}:{line_number}"))
            yield _document_from_text(row_source, title, text, str(item.get("url", "")))


def load_documents(corpus_path: Path) -> list[Document]:
    corpus_path = corpus_path.resolve()
    if not corpus_path.exists():
        raise FileNotFoundError(f"Corpus path does not exist: {corpus_path}")
    if corpus_path.is_file():
        paths = [corpus_path]
        root = corpus_path.parent
    else:
        paths = sorted(
            p for p in corpus_path.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        )
        root = corpus_path

    documents: list[Document] = []
    for path in paths:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        source = path.relative_to(root).as_posix()
        if path.suffix.lower() == ".jsonl":
            documents.extend(_read_jsonl(path, source))
            continue
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        text = _clean_html(raw) if path.suffix.lower() in {".html", ".htm"} else raw
        documents.append(_document_from_text(source, path.stem, text))

    if not documents:
        raise ValueError(f"No supported text documents found under {corpus_path}")
    doc_ids = [doc.doc_id for doc in documents]
    if len(doc_ids) != len(set(doc_ids)):
        raise ValueError("Duplicate document IDs; provide distinct source paths or contents")
    return documents


def _heading_at(text: str, position: int) -> str:
    heading = ""
    for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text[:position]):
        heading = match.group(1).strip()
    return heading


def split_document(document: Document, chunk_chars: int = 1800, overlap_chars: int = 180) -> list[Chunk]:
    if chunk_chars < 100:
        raise ValueError("chunk_chars must be at least 100")
    if overlap_chars < 0 or overlap_chars >= chunk_chars:
        raise ValueError("overlap_chars must be nonnegative and smaller than chunk_chars")

    text = document.text
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + chunk_chars)
        end = hard_end
        if hard_end < len(text):
            # Prefer a paragraph or line boundary, but avoid tiny fragments.
            lower_bound = start + chunk_chars // 2
            paragraph = text.rfind("\n\n", lower_bound, hard_end)
            line = text.rfind("\n", lower_bound, hard_end)
            boundary = paragraph + 2 if paragraph >= 0 else line + 1 if line >= 0 else -1
            if boundary > start:
                end = boundary
        raw_piece = text[start:end]
        left_trim = len(raw_piece) - len(raw_piece.lstrip())
        right_trim = len(raw_piece.rstrip())
        piece = raw_piece.strip()
        if piece:
            piece_start = start + left_trim
            piece_end = start + right_trim
            chunk_id = f"CHK-{_digest(f'{document.doc_id}:{piece_start}:{piece_end}:{piece}', 16)}"
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_id=document.doc_id,
                    source=document.source,
                    title=document.title,
                    text=piece,
                    start_char=piece_start,
                    end_char=piece_end,
                    heading=_heading_at(text, piece_start),
                    url=document.url,
                )
            )
        if end >= len(text):
            break
        next_start = end - overlap_chars
        start = max(start + 1, next_start)
    if not chunks:
        raise ValueError(f"Document yielded no chunks: {document.source}")
    return chunks


def chunk_documents(
    documents: list[Document], chunk_chars: int = 1800, overlap_chars: int = 180
) -> list[Chunk]:
    result = [chunk for doc in documents for chunk in split_document(doc, chunk_chars, overlap_chars)]
    chunk_ids = [chunk.chunk_id for chunk in result]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("Chunk IDs are not unique")
    return result
