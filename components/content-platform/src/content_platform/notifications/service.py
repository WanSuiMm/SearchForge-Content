"""Read-only notification service scoped to the authenticated recipient."""

from ..errors import ValidationError
from ..storage.repositories import NotificationRepository


class NotificationService:
    def __init__(self, repository: NotificationRepository) -> None:
        self.repository = repository

    def list_for_user(self, user_id: str, *, limit: int = 20) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValidationError("limit must be an integer from 1 to 100")
        return self.repository.list_for_user(user_id, limit=limit)
