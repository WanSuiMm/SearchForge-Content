from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Document:
    doc_id: str
    source: str
    title: str
    text: str
    url: str = ""


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    source: str
    title: str
    text: str
    start_char: int
    end_char: int
    heading: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Chunk":
        return cls(**value)


@dataclass
class SearchHit:
    chunk: Chunk
    score: float
    dense_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    dense_rank: int | None = None
    bm25_rank: int | None = None
    rerank_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["chunk"] = self.chunk.to_dict()
        return result


@dataclass(frozen=True)
class PlannedSection:
    title: str
    purpose: str
    queries: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    section: str
    text: str
    cited_chunk_ids: tuple[str, ...]


def jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")
