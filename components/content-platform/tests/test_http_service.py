"""Real loopback HTTP integration checks for authentication and service lifecycle."""

from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from content_platform.api.http_service import (  # noqa: E402
    ContentPlatformHTTPServer,
    TokenBucketLimiter,
)
from content_platform.application import create_application  # noqa: E402
from content_platform.cache.local import LocalVersionedTTLCache  # noqa: E402
from content_platform.errors import CacheUnavailable  # noqa: E402


class OfflineCache:
    def get(self, namespace: str, key: str) -> object | None:
        raise CacheUnavailable("fixture cache is offline")

    def set(self, namespace: str, key: str, value: object, *, ttl_seconds: float) -> None:
        raise CacheUnavailable("fixture cache is offline")

    def invalidate(self, namespace: str) -> None:
        raise CacheUnavailable("fixture cache is offline")


class HTTPServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=PROJECT_ROOT)
        self.addCleanup(self.tempdir.cleanup)
        self.database_path = Path(self.tempdir.name) / "http.sqlite3"
        self.app = create_application(
            self.database_path,
            initialize=True,
            cache=LocalVersionedTTLCache(max_entries=128),
        )
        self.server = ContentPlatformHTTPServer(
            ("127.0.0.1", 0), self.app, max_concurrent_requests=8
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="content-platform-http-test",
            daemon=False,
        )
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.close_server)

    def close_server(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()

    def enable_projection_service(self) -> None:
        self.close_server()
        self.server = ContentPlatformHTTPServer(
            ("127.0.0.1", 0),
            self.app,
            max_concurrent_requests=8,
            projection_token="projection-test-secret",
            projection_consumer_id="searchforge-content-v1",
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="content-platform-projection-http-test",
            daemon=False,
        )
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, object] | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request_headers = dict(headers or {})
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request = Request(
            self.endpoint + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=4) as response:
                status = response.status
                payload = response.read()
        except HTTPError as exc:
            status = exc.code
            payload = exc.read()
        except URLError as exc:
            if isinstance(exc.reason, PermissionError):
                self.skipTest("the current sandbox does not permit loopback socket connections")
            raise
        parsed = json.loads(payload.decode("utf-8")) if payload else {}
        return status, parsed

    def register(self, user_id: str) -> str:
        status, response = self.request(
            "POST",
            "/v1/users",
            body={
                "user_id": user_id,
                "display_name": user_id.title(),
                "password": f"{user_id}-strong-password",
            },
        )
        self.assertEqual(status, 201, response)
        token = response["access_token"]
        self.assertIsInstance(token, str)
        return str(token)

    def test_projection_routes_are_disabled_by_default_and_use_a_dedicated_token(self) -> None:
        self.assertEqual(
            self.request(
                "GET",
                "/internal/v1/content-projection/events?consumer_id=searchforge-content-v1",
            )[0],
            404,
        )

        self.enable_projection_service()
        user_token = self.register("projection-http-user")
        self.app.content.publish("projection-http-user", "internal projection event", "http-projection")

        path = "/internal/v1/content-projection/events?consumer_id=searchforge-content-v1"
        self.assertEqual(self.request("GET", path, token=user_token)[0], 401)
        self.assertEqual(self.request("GET", path, token="wrong-projection-secret")[0], 401)
        status, response = self.request("GET", path, token="projection-test-secret")
        self.assertEqual(status, 200)
        self.assertEqual(len(response["events"]), 1)
        self.assertEqual(response["events"][0]["event_type"], "content.published")
        self.assertEqual(
            self.request(
                "GET",
                "/internal/v1/content-projection/events?consumer_id=other-consumer",
                token="projection-test-secret",
            )[0],
            403,
        )

    def test_projection_content_ack_and_invalid_body_contract(self) -> None:
        self.enable_projection_service()
        self.app.identity.create_user("projection-source-user", "Projection Source User")
        published = self.app.content.publish(
            "projection-source-user", "source content for C3", "projection-http-ack"
        )
        token = "projection-test-secret"
        events_path = "/internal/v1/content-projection/events?consumer_id=searchforge-content-v1"
        status, events_response = self.request("GET", events_path, token=token)
        self.assertEqual(status, 200)
        event = events_response["events"][0]

        content_status, document = self.request(
            "GET",
            f"/internal/v1/content-projection/content/{published.content.content_id}",
            token=token,
        )
        self.assertEqual(content_status, 200)
        self.assertEqual(document["body"], "source content for C3")
        self.assertEqual(document["source_uri"], event["source_uri"])

        ack_path = "/internal/v1/content-projection/ack"
        self.assertEqual(
            self.request("POST", ack_path, token=token, body={"consumer_id": "searchforge-content-v1"})[0],
            400,
        )
        self.assertEqual(
            self.request(
                "POST",
                ack_path,
                token=token,
                body={"consumer_id": "other-consumer", "event_id": event["event_id"]},
            )[0],
            403,
        )
        ack_status, ack = self.request(
            "POST",
            ack_path,
            token=token,
            body={"consumer_id": "searchforge-content-v1", "event_id": event["event_id"]},
        )
        self.assertEqual(ack_status, 200)
        self.assertTrue(ack["acknowledged"])
        self.assertEqual(self.request("GET", events_path, token=token)[1]["events"], [])
        self.assertEqual(
            self.request("GET", "/internal/v1/content-projection/content/missing", token=token)[0],
            404,
        )

    def test_projection_bulk_resolve_is_bounded_and_uses_the_dedicated_token(self) -> None:
        self.enable_projection_service()
        self.app.identity.create_user("projection-bulk-user", "Projection Bulk User")
        first = self.app.content.publish(
            "projection-bulk-user", "first resolved body", "projection-bulk-http-first"
        )
        second = self.app.content.publish(
            "projection-bulk-user", "second resolved body", "projection-bulk-http-second"
        )
        path = "/internal/v1/content-projection/resolve"
        token = "projection-test-secret"

        status, response = self.request(
            "POST",
            path,
            token=token,
            body={
                "content_ids": [second.content.content_id, "missing-content", first.content.content_id]
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["content_id"] for item in response["items"]],
            [second.content.content_id, first.content.content_id],
        )

        self.assertEqual(
            self.request("POST", path, token=token, body={"content_ids": ["same", "same"]})[0],
            400,
        )
        self.assertEqual(
            self.request(
                "POST",
                path,
                token=token,
                body={"content_ids": [f"content-{index}" for index in range(21)]},
            )[0],
            400,
        )
        self.assertEqual(
            self.request("POST", path, token=token, body={"content_ids": [], "extra": True})[0],
            400,
        )
        self.assertEqual(
            self.request(
                "POST",
                path,
                token="wrong-projection-secret",
                body={"content_ids": [first.content.content_id]},
            )[0],
            401,
        )

    def test_authenticated_two_user_publish_follow_search_and_cache_invalidation(self) -> None:
        author_token = self.register("author-http")
        reader_token = self.register("reader-http")

        self.assertEqual(self.request("GET", "/v1/feed")[0], 401)
        self.assertEqual(
            self.request(
                "POST", "/v1/follows", token=reader_token,
                body={"followee_id": "author-http"},
            )[0],
            200,
        )
        empty_status, empty_feed = self.request("GET", "/v1/feed", token=reader_token)
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_feed["items"], [])

        spoof_status, _ = self.request(
            "POST", "/v1/content", token=author_token,
            body={"body": "indexed service transaction", "author_id": "reader-http"},
            headers={"Idempotency-Key": "spoof-attempt"},
        )
        self.assertEqual(spoof_status, 400)

        status, created = self.request(
            "POST", "/v1/content", token=author_token,
            body={"body": "indexed service transaction"},
            headers={"Idempotency-Key": "http-publish-1"},
        )
        self.assertEqual(status, 201, created)
        self.assertEqual(created["content"]["author_id"], "author-http")

        replay_status, replay = self.request(
            "POST", "/v1/content", token=author_token,
            body={"body": "indexed service transaction"},
            headers={"Idempotency-Key": "http-publish-1"},
        )
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["content"]["content_id"], created["content"]["content_id"])

        conflict_status, _ = self.request(
            "POST", "/v1/content", token=author_token,
            body={"body": "different payload"},
            headers={"Idempotency-Key": "http-publish-1"},
        )
        self.assertEqual(conflict_status, 409)
        with self.app.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0], 1)

        feed_status, feed = self.request("GET", "/v1/feed", token=reader_token)
        self.assertEqual(feed_status, 200)
        self.assertEqual([item["content_id"] for item in feed["items"]], [created["content"]["content_id"]])
        search_status, search = self.request(
            "GET", "/v1/search?q=indexed%20service", token=reader_token
        )
        self.assertEqual(search_status, 200)
        self.assertEqual(search["items"][0]["content_id"], created["content"]["content_id"])
        self.assertEqual(self.request("GET", "/v1/feed?user_id=author-http", token=reader_token)[0], 400)
        self.assertEqual(self.request("GET", "/v1/me", token=reader_token)[1]["user_id"], "reader-http")

    def test_login_revocation_and_only_hashed_credentials_are_persisted(self) -> None:
        registration_token = self.register("account-http")
        valid = "account-http-strong-password"
        login_status, login = self.request(
            "POST", "/v1/sessions", body={"user_id": "account-http", "password": valid}
        )
        self.assertEqual(login_status, 200)
        self.assertNotEqual(login["access_token"], registration_token)
        invalid_status, _ = self.request(
            "POST", "/v1/sessions", body={"user_id": "account-http", "password": "wrong-password-value"}
        )
        self.assertEqual(invalid_status, 401)
        self.assertEqual(self.request("GET", "/v1/me", token=login["access_token"])[0], 200)
        self.assertEqual(
            self.request("DELETE", "/v1/sessions/current", token=login["access_token"])[0],
            200,
        )
        self.assertEqual(self.request("GET", "/v1/me", token=login["access_token"])[0], 401)
        with self.app.database.connect() as connection:
            credential = connection.execute(
                "SELECT password_salt, password_hash FROM auth_credentials WHERE user_id = ?",
                ("account-http",),
            ).fetchone()
            session_hashes = [row[0] for row in connection.execute("SELECT token_hash FROM auth_sessions")]
        self.assertNotEqual(credential["password_hash"], valid)
        self.assertTrue(all(len(token_hash) == 64 for token_hash in session_hashes))
        self.assertNotIn(registration_token, session_hashes)

    def test_background_outbox_worker_delivers_user_scoped_notifications_and_stops(self) -> None:
        author_token = self.register("author-notify")
        reader_token = self.register("reader-notify")
        self.assertEqual(
            self.request(
                "POST", "/v1/follows", token=reader_token,
                body={"followee_id": "author-notify"},
            )[0],
            200,
        )
        published_status, published = self.request(
            "POST", "/v1/content", token=author_token,
            body={"body": "worker notification body"},
            headers={"Idempotency-Key": "notification-publish"},
        )
        self.assertEqual(published_status, 201)

        notifications: list[dict[str, object]] = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, response = self.request("GET", "/v1/notifications", token=reader_token)
            self.assertEqual(status, 200)
            notifications = response["items"]
            if notifications:
                break
            time.sleep(0.02)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["content_id"], published["content"]["content_id"])
        self.assertEqual(self.request("GET", "/v1/notifications", token=author_token)[1]["items"], [])
        self.assertTrue(self.server.worker.is_alive)
        self.close_server()
        self.assertFalse(self.server.worker.is_alive)
        self.assertFalse(self.thread.is_alive())

    def test_cache_outage_bypasses_reads_and_does_not_undo_committed_writes(self) -> None:
        self.server.application = create_application(
            self.database_path,
            cache=OfflineCache(),
        )
        author_token = self.register("author-cache-offline")
        reader_token = self.register("reader-cache-offline")
        follow_status, _ = self.request(
            "POST",
            "/v1/follows",
            token=reader_token,
            body={"followee_id": "author-cache-offline"},
        )
        self.assertEqual(follow_status, 200)
        publish_status, published = self.request(
            "POST",
            "/v1/content",
            token=author_token,
            body={"body": "content remains readable through SQLite during cache outage"},
            headers={"Idempotency-Key": "cache-outage-publish"},
        )
        self.assertEqual(publish_status, 201)
        feed_status, feed = self.request("GET", "/v1/feed", token=reader_token)
        self.assertEqual(feed_status, 200)
        self.assertEqual(feed["items"][0]["content_id"], published["content"]["content_id"])

        metrics_status, metrics = self.request("GET", "/metrics")
        self.assertEqual(metrics_status, 200)
        counters = metrics["counters"]
        self.assertGreaterEqual(counters["cache_errors"], 3)
        self.assertGreaterEqual(counters["cache_invalidation_failures"], 2)
        self.assertGreaterEqual(counters["cache_bypasses"], 1)

    def test_request_size_rate_limit_concurrency_limit_and_metrics(self) -> None:
        too_large = b"{" + b"a" * (64 * 1024) + b"}"
        self.assertEqual(
            self.request(
                "POST", "/v1/users", raw_body=too_large,
                headers={"Content-Type": "application/json"},
            )[0],
            413,
        )

        self.server.rate_limiter = TokenBucketLimiter(capacity=1, refill_per_second=0.01)
        self.assertEqual(self.request("GET", "/v1/me")[0], 401)
        self.assertEqual(self.request("GET", "/v1/me")[0], 429)

        held_slots = 0
        try:
            # The client can receive the response just before the server request
            # thread exits and returns its semaphore slot. Wait for all earlier
            # requests to finish before asserting the configured capacity.
            deadline = time.monotonic() + 2
            while held_slots < 8:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.server._request_slots.acquire(
                    timeout=remaining
                ):
                    break
                held_slots += 1
            self.assertEqual(held_slots, 8)
            self.assertEqual(self.request("GET", "/healthz")[0], 503)
        finally:
            for _ in range(held_slots):
                self.server._request_slots.release()
        metrics_status, metrics = self.request("GET", "/metrics")
        self.assertEqual(metrics_status, 200)
        self.assertGreaterEqual(metrics["requests_total"], 4)
        self.assertEqual(metrics["counters"]["concurrency_rejections"], 1)
        self.assertIn("p99", metrics["latency_ms"])


if __name__ == "__main__":
    unittest.main()
