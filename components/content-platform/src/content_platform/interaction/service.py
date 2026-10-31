"""Like service. Database triggers keep stored counts aligned with unique rows."""

from collections.abc import Callable

from ..contracts import LikeResult
from ..errors import ValidationError
from ..storage.repositories import InteractionRepository
from ..timeutil import utc_timestamp


class InteractionService:
    def __init__(
        self,
        repository: InteractionRepository,
        *,
        clock: Callable[[], str] = utc_timestamp,
    ) -> None:
        self.repository = repository
        self.clock = clock

    def like(self, user_id: str, content_id: str) -> LikeResult:
        if not user_id or not content_id:
            raise ValidationError("user_id and content_id are required")
        return self.repository.like(user_id, content_id, self.clock())
