"""Framework-neutral API facade. HTTP transport is intentionally out of scope."""

from ..contracts import ContentRecord, FeedPage, LikeResult, PublishedContent
from ..content.service import ContentService
from ..feed.service import FeedService
from ..identity.service import IdentityService
from ..interaction.service import InteractionService
from ..notifications.service import NotificationService
from ..search.service import SearchService


def _content_json(content: ContentRecord) -> dict[str, object]:
    return {
        "content_id": content.content_id,
        "author_id": content.author_id,
        "author_name": content.author_name,
        "body": content.body,
        "published_at": content.published_at,
        "like_count": content.like_count,
    }


class ContentPlatformAPI:
    def __init__(
        self,
        identity: IdentityService,
        content: ContentService,
        feed: FeedService,
        interaction: InteractionService,
        search: SearchService,
        notifications: NotificationService,
    ) -> None:
        self.identity = identity
        self.content = content
        self.feed = feed
        self.interaction = interaction
        self.search = search
        self.notifications = notifications

    def create_user(self, user_id: str, display_name: str) -> dict[str, object]:
        self.identity.create_user(user_id, display_name)
        return {"user_id": user_id, "display_name": display_name}

    def follow(self, follower_id: str, followee_id: str) -> dict[str, object]:
        created = self.identity.follow(follower_id, followee_id)
        return {"follower_id": follower_id, "followee_id": followee_id, "created": created}

    def publish(
        self, author_id: str, body: str, idempotency_key: str
    ) -> dict[str, object]:
        result: PublishedContent = self.content.publish(author_id, body, idempotency_key)
        return {"content": _content_json(result.content), "replayed": result.replayed}

    def get_feed(
        self,
        user_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, object]:
        page: FeedPage = self.feed.get_feed(user_id, limit=limit, cursor=cursor)
        return {
            "items": [_content_json(item) for item in page.items],
            "next_cursor": page.next_cursor,
        }

    def like(self, user_id: str, content_id: str) -> dict[str, object]:
        result: LikeResult = self.interaction.like(user_id, content_id)
        return {"liked": result.liked, "like_count": result.like_count}

    def search_content(self, query: str, *, limit: int = 20) -> dict[str, object]:
        items = self.search.search(query, limit=limit)
        return {"items": [_content_json(item) for item in items]}

    def get_notifications(self, user_id: str, *, limit: int = 20) -> dict[str, object]:
        return {"items": self.notifications.list_for_user(user_id, limit=limit)}
