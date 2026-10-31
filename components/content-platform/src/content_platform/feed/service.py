"""Feed query service using keyset pagination."""

from ..contracts import FeedPage
from ..errors import ValidationError
from ..storage.repositories import FeedRepository
from .cursor import decode_cursor, encode_cursor


class FeedService:
    def __init__(self, repository: FeedRepository) -> None:
        self.repository = repository

    def get_feed(
        self,
        user_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> FeedPage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValidationError("limit must be an integer from 1 to 100")
        after = decode_cursor(cursor)
        rows = self.repository.page(user_id=user_id, limit=limit + 1, after=after)
        has_more = len(rows) > limit
        items = tuple(rows[:limit])
        next_cursor = (
            encode_cursor(items[-1].published_at, items[-1].content_id)
            if has_more and items
            else None
        )
        return FeedPage(items=items, next_cursor=next_cursor)
