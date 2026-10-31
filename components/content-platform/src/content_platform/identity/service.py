"""User and follow operations."""

from collections.abc import Callable

from ..errors import ValidationError
from ..storage.repositories import UserRepository
from ..timeutil import utc_timestamp


def _required_text(value: str, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValidationError(f"{field} must contain 1 to {max_length} characters")
    return normalized


class IdentityService:
    def __init__(
        self,
        repository: UserRepository,
        *,
        clock: Callable[[], str] = utc_timestamp,
    ) -> None:
        self.repository = repository
        self.clock = clock

    def create_user(self, user_id: str, display_name: str) -> None:
        user_id = _required_text(user_id, "user_id", 100)
        display_name = _required_text(display_name, "display_name", 200)
        self.repository.create_user(user_id, display_name, self.clock())

    def follow(self, follower_id: str, followee_id: str) -> bool:
        follower_id = _required_text(follower_id, "follower_id", 100)
        followee_id = _required_text(followee_id, "followee_id", 100)
        if follower_id == followee_id:
            raise ValidationError("a user cannot follow themselves")
        return self.repository.follow(follower_id, followee_id, self.clock())
