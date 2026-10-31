"""At-least-once projection relay and authoritative read validation."""
from __future__ import annotations

from typing import Protocol

from .source import ProjectionSource, SourceUnavailable, validate_consumer


class ProjectionIndex(Protocol):
    def apply(self, event: dict) -> dict: ...
    def search(self, query: str, *, limit: int = 20) -> list[dict]: ...


class ProjectionRelay:
    def __init__(self, source: ProjectionSource, index: ProjectionIndex, *, consumer_id: str = "searchforge-content-v1") -> None:
        self.source = source
        self.index = index
        self.consumer_id = validate_consumer(consumer_id)

    def run_once(self, *, limit: int = 100) -> dict:
        """Apply each event durably before acknowledgement; stop on any failure.

        A crash or lost ack causes replay. Index event receipts make that replay
        idempotent. We never claim distributed exactly-once delivery.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("batch limit must be an integer from 1 to 100")
        events = self.source.pending(self.consumer_id, limit=limit)
        counts = {"applied": 0, "duplicate": 0, "stale": 0, "acknowledged": 0}
        for event in events:
            result = self.index.apply(event)
            outcome = result.get("outcome")
            if outcome not in {"applied", "duplicate", "stale"} or result.get("event_id") != event.get("event_id"):
                raise RuntimeError("projection index returned an invalid receipt")
            counts[outcome] += 1
            if not self.source.acknowledge(self.consumer_id, result["event_id"]):
                raise SourceUnavailable("source did not confirm the projection acknowledgement; replay is safe")
            counts["acknowledged"] += 1
        return {"status": "BATCH_COMPLETED", "consumer_id": self.consumer_id,
                "received": len(events), **counts}


class PublicContentSearch:
    """Filter every projected hit through the authoritative source before return.

    A lagging index may temporarily omit results. It must not reveal a content
    body after source revocation, or return an obsolete version as current.
    """

    def __init__(self, source: ProjectionSource, index: ProjectionIndex) -> None:
        self.source = source
        self.index = index

    @staticmethod
    def _public(value: dict | None, content_id: str) -> dict | None:
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get("content_id") != content_id
                or value.get("visibility") != "public"
                or isinstance(value.get("version"), bool) or not isinstance(value.get("version"), int)
                or value["version"] < 1 or not isinstance(value.get("body"), str)
                or not isinstance(value.get("author_id"), str)
                or not isinstance(value.get("published_at"), str)
                or value.get("source_uri") != "/v1/content/" + content_id):
            raise SourceUnavailable("source returned a document outside the public-content contract")
        # Only named public fields cross the service boundary.
        return {key: value[key] for key in (
            "content_id", "author_id", "body", "published_at", "version", "visibility", "source_uri",
        )}

    def search(self, query: str, *, limit: int = 20) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("public search limit must be an integer from 1 to 20")
        hits = self.index.search(query, limit=limit)
        # One bounded source request replaces an N-per-hit request fan-out.
        documents = self.source.get_public_many([hit["content_id"] for hit in hits]) if hits else []
        current_by_id = {value["content_id"]: value for value in documents}
        items = []
        filtered = 0
        for hit in hits:
            current = self._public(current_by_id.get(hit["content_id"]), hit["content_id"])
            if current is None or current["version"] != hit["version"] or current["body"] != hit["body"]:
                filtered += 1
                continue
            items.append({**current, "score": hit["score"]})
        return {"items": items, "filtered_candidates": filtered,
                "backend": "sqlite-fts5-bm25", "source_validated": True}

    def detail(self, content_id: str) -> dict | None:
        return self._public(self.source.get_public(content_id), content_id)
