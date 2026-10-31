"""Content publishing service with request-scoped idempotency."""

from collections.abc import Callable
import hashlib
import json

from ..contracts import PublishedContent
from ..errors import ValidationError
from ..storage.repositories import ContentRepository
from ..timeutil import utc_timestamp


class ContentService:
    def __init__(
        self,
        repository: ContentRepository,
        *,
        clock: Callable[[], str] = utc_timestamp,
    ) -> None:
        self.repository = repository
        self.clock = clock

    def publish(self, author_id: str, body: str, idempotency_key: str) -> PublishedContent:
        if not isinstance(author_id, str) or not author_id.strip() or len(author_id) > 100:
            raise ValidationError("author_id must contain 1 to 100 characters")
        if not isinstance(body, str) or not body.strip() or len(body) > 20_000:
            raise ValidationError("body must contain 1 to 20000 characters")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 200
        ):
            raise ValidationError("idempotency_key must contain 1 to 200 characters")
        body = body.strip()
        idempotency_key = idempotency_key.strip()
        canonical_request = json.dumps(
            {"body": body}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        return self.repository.publish_with_outbox(
            author_id=author_id.strip(),
            body=body,
            published_at=self.clock(),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
