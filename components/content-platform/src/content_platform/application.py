"""Explicit dependency wiring for the offline ContentPlatform."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from .api.facade import ContentPlatformAPI
from .cache.port import CachePort
from .content.service import ContentService
from .feed.service import FeedService
from .identity.auth import AuthRepository, AuthService
from .identity.service import IdentityService
from .interaction.service import InteractionService
from .jobs.consumer import NotificationConsumer
from .jobs.outbox import OutboxRepository
from .storage.database import Database
from .storage.repositories import (
    ContentRepository,
    FeedRepository,
    InteractionRepository,
    NotificationRepository,
    SearchRepository,
    UserRepository,
)
from .notifications.service import NotificationService
from .search.service import SearchService
from .timeutil import utc_timestamp


@dataclass(frozen=True)
class ContentPlatformApplication:
    database: Database
    api: ContentPlatformAPI
    auth: AuthService
    identity: IdentityService
    content: ContentService
    feed: FeedService
    interaction: InteractionService
    search: SearchService
    notifications: NotificationService
    outbox: OutboxRepository
    consumer: NotificationConsumer
    cache: CachePort | None


def create_application(
    database_path: str | Path,
    *,
    initialize: bool = False,
    cache: CachePort | None = None,
    clock: Callable[[], str] = utc_timestamp,
) -> ContentPlatformApplication:
    """Wire services without touching the database unless initialize=True."""
    database = Database(database_path)
    if initialize:
        database.initialize()
    identity = IdentityService(UserRepository(database), clock=clock)
    content = ContentService(ContentRepository(database), clock=clock)
    feed = FeedService(FeedRepository(database))
    interaction = InteractionService(InteractionRepository(database), clock=clock)
    search = SearchService(SearchRepository(database))
    notifications = NotificationService(NotificationRepository(database))
    auth = AuthService(AuthRepository(database), clock=clock)
    outbox = OutboxRepository(database)
    consumer = NotificationConsumer(database)
    api = ContentPlatformAPI(identity, content, feed, interaction, search, notifications)
    return ContentPlatformApplication(
        database=database,
        api=api,
        auth=auth,
        identity=identity,
        content=content,
        feed=feed,
        interaction=interaction,
        search=search,
        notifications=notifications,
        outbox=outbox,
        consumer=consumer,
        cache=cache,
    )
