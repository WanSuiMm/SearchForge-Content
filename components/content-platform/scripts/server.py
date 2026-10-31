"""Run the local ContentPlatform HTTP API with explicit database settings."""

import argparse
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from content_platform.api.http_service import ContentPlatformHTTPServer  # noqa: E402
from content_platform.application import create_application  # noqa: E402
from content_platform.cache.local import LocalVersionedTTLCache  # noqa: E402
from content_platform.cache.redis import RedisVersionedTTLCache  # noqa: E402
from content_platform.errors import CacheUnavailable  # noqa: E402
from content_platform.projection import DEFAULT_PROJECTION_CONSUMER_ID  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local ContentPlatform HTTP API.")
    parser.add_argument("--database", type=Path, help="SQLite database path")
    parser.add_argument(
        "--database-backend",
        choices=("sqlite", "postgresql"),
        default="sqlite",
        help="sqlite is the default; postgresql reads CONTENTPLATFORM_DATABASE_URL",
    )
    parser.add_argument("--initialize", action="store_true", help="create/apply selected database migrations")
    parser.add_argument("--host", default="127.0.0.1", help="bind address; loopback is the default")
    parser.add_argument("--port", default=8000, type=int, help="TCP port (0 selects an ephemeral port)")
    parser.add_argument("--max-concurrent-requests", default=32, type=int)
    parser.add_argument("--rate-capacity", default=120, type=float)
    parser.add_argument("--rate-refill-per-second", default=60, type=float)
    parser.add_argument(
        "--cache",
        choices=("on", "off", "redis"),
        default="on",
        help="on selects the process-local cache; redis selects the configured Redis endpoint",
    )
    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", default=6379, type=int)
    parser.add_argument("--redis-database", default=0, type=int)
    parser.add_argument("--redis-key-prefix", default="content-platform")
    parser.add_argument("--redis-tls", action="store_true", help="verify Redis with the system CA trust store")
    parser.add_argument(
        "--event-delivery",
        choices=("in-process", "kafka"),
        default="in-process",
        help="kafka reserves the transactional Outbox for the external Kafka bridge; it does not fall back locally",
    )
    parser.add_argument(
        "--projection-token-env",
        help="environment variable containing the dedicated internal projection bearer token; disabled by default",
    )
    parser.add_argument(
        "--projection-consumer-id",
        default=DEFAULT_PROJECTION_CONSUMER_ID,
        help="single consumer allowed to read and acknowledge projection events",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.database_backend == "sqlite":
        if args.database is None:
            parser.error("--database is required when --database-backend=sqlite")
        database_reference: str | Path = args.database.resolve()
        if not args.initialize and not Path(database_reference).is_file():
            parser.error("database does not exist; pass --initialize for a new database")
    else:
        if args.database is not None:
            parser.error("--database is only used by SQLite; set CONTENTPLATFORM_DATABASE_URL for PostgreSQL")
        database_reference = os.environ.get("CONTENTPLATFORM_DATABASE_URL", "")
        if not database_reference.startswith(("postgresql://", "postgres://")):
            parser.error("PostgreSQL requires CONTENTPLATFORM_DATABASE_URL with a postgresql:// URL")
    projection_token = None
    if args.projection_token_env is not None:
        if not args.projection_token_env or args.projection_token_env != args.projection_token_env.strip():
            parser.error("--projection-token-env must name an environment variable")
        projection_token = os.environ.get(args.projection_token_env)
        if not projection_token:
            parser.error("the configured projection token environment variable is missing or empty")
    if args.cache == "on":
        cache = LocalVersionedTTLCache()
    elif args.cache == "redis":
        cache = RedisVersionedTTLCache(
            args.redis_host,
            args.redis_port,
            database=args.redis_database,
            username=os.environ.get("CONTENTPLATFORM_REDIS_USERNAME"),
            password=os.environ.get("CONTENTPLATFORM_REDIS_PASSWORD"),
            key_prefix=args.redis_key_prefix,
            tls=args.redis_tls,
        )
        try:
            cache.ping()
        except CacheUnavailable:
            parser.error("Redis was selected but its health check failed; no local-cache fallback is enabled")
    else:
        cache = None
    app = create_application(database_reference, initialize=args.initialize, cache=cache)
    server = ContentPlatformHTTPServer(
        (args.host, args.port),
        app,
        max_concurrent_requests=args.max_concurrent_requests,
        rate_capacity=args.rate_capacity,
        rate_refill_per_second=args.rate_refill_per_second,
        static_root=PROJECT_ROOT / "web",
        start_outbox_worker=args.event_delivery == "in-process",
        projection_token=projection_token,
        projection_consumer_id=args.projection_consumer_id,
    )
    host, port = server.server_address[:2]
    print(f"ContentPlatform HTTP service listening at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping ContentPlatform HTTP service.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
