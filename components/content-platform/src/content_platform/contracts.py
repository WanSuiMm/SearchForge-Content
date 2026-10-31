"""Small immutable records exchanged between repositories and services."""

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class RunContext:
    run_id: str
    trace_id: str
    config: Mapping[str, object]


class ConfigurablePort(Protocol):
    """A future adapter validates configuration without starting a workload."""

    def validate_config(self, config: Mapping[str, object]) -> None: ...


class EventEnvelope(Protocol):
    """Business event fields shared by SQLite and external transport records."""

    event_id: str
    event_type: str
    payload: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class ContentRecord:
    content_id: str
    author_id: str
    author_name: str
    body: str
    published_at: str
    like_count: int


@dataclass(frozen=True)
class PublishedContent:
    content: ContentRecord
    replayed: bool


@dataclass(frozen=True)
class FeedPage:
    items: tuple[ContentRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class LikeResult:
    liked: bool
    like_count: int


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    event_type: str
    payload: dict[str, object]
    created_at: str
    attempts: int
    lease_owner: str
    lease_until: float
