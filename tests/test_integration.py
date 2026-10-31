"""Fresh-database functional checks across two actual loopback HTTP services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "components/searchforge" / "src"),
               str(ROOT / "components/content-platform" / "src")]

from content_platform.api.http_service import ContentPlatformHTTPServer
from content_platform.application import create_application
from searchforge.community import CommunityIndex
from searchforge_content.http_service import ContentSearchHTTPServer
from searchforge_content.runtime import ProjectionRelay, PublicContentSearch
from searchforge_content.source import HTTPProjectionSource, SourceUnavailable


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="content-integration-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = create_application(self.directory / "source.sqlite3", initialize=True)
        self.service_token = secrets.token_urlsafe(32)
        self.source_server = ContentPlatformHTTPServer(
            ("127.0.0.1", 0), self.app, start_outbox_worker=False,
            projection_token=self.service_token, projection_consumer_id="searchforge-content-v1",
        )
        self.source_url = self.start_server(self.source_server)
        self.source = HTTPProjectionSource(self.source_url, self.service_token, timeout_seconds=1)
        self.index_path = self.directory / "index.sqlite3"
        self.index = CommunityIndex(self.index_path)
        self.index.initialize()
        self.addCleanup(self.index.close)
        self.relay = ProjectionRelay(self.source, self.index)
        self.search = PublicContentSearch(self.source, self.index)
        self.search_url = self.start_server(ContentSearchHTTPServer(("127.0.0.1", 0), self.search))

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=False)
        thread.start()

        def close():
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            self.assertFalse(thread.is_alive(), "test server did not stop")

        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}"

    def request(self, origin, method, path, *, body=None, token=None, key=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        request = Request(origin + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers, method=method)
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read())

    def publish(self, body="Agent workflow provenance and durable content search"):
        status, account = self.request(self.source_url, "POST", "/v1/users", body={
            "user_id": "author", "display_name": "Test author", "password": "test-only-long-password",
        })
        self.assertEqual(status, 201, account)
        token = account["access_token"]
        status, result = self.request(self.source_url, "POST", "/v1/content", token=token,
                                      body={"body": body}, key="publish-1")
        self.assertEqual(status, 201, result)
        return token, result

    def test_publish_notification_index_search_and_detail_over_http(self):
        token, published = self.publish()
        identifier = published["content"]["content_id"]
        # Draining the existing notification Outbox must not consume search's events.
        for event in self.app.outbox.claim("notification-check"):
            self.app.consumer.consume(event)
            self.assertTrue(self.app.outbox.ack(event.event_id, "notification-check"))
        self.assertEqual(self.app.outbox.count(status="delivered"), 1)
        self.assertEqual(len(self.source.pending("searchforge-content-v1")), 1)
        self.assertEqual(self.relay.run_once()["applied"], 1)
        self.assertEqual(self.source.pending("searchforge-content-v1"), [])
        status, result = self.request(self.search_url, "GET", "/v1/search?q=durable%20content")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["source_validated"])
        self.assertEqual([hit["content_id"] for hit in result["items"]], [identifier])
        hit = result["items"][0]
        self.assertEqual(hit["version"], 1)
        status, detail = self.request(self.search_url, "GET", hit["source_uri"])
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["body"], published["content"]["body"])
        status, liked = self.request(self.source_url, "POST", f"/v1/content/{identifier}/likes", token=token)
        self.assertEqual(status, 200, liked)
        self.assertEqual(liked["like_count"], 1)
        self.assertEqual(self.relay.run_once()["received"], 0)

    def test_idempotent_publication_emits_only_one_search_event(self):
        token, published = self.publish()
        status, replay = self.request(self.source_url, "POST", "/v1/content", token=token,
                                     body={"body": published["content"]["body"]}, key="publish-1")
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["content"]["content_id"], published["content"]["content_id"])
        self.assertEqual(len(self.source.pending("searchforge-content-v1")), 1)
        self.assertEqual(self.relay.run_once()["acknowledged"], 1)

    def test_lost_ack_replays_after_index_restart_without_duplicate_documents(self):
        self.publish()
        actual_source = self.source

        class LostAck:
            pending = actual_source.pending
            get_public = actual_source.get_public

            def acknowledge(self, consumer_id, event_id):
                raise SourceUnavailable("injected lost acknowledgement before source commit")

        with self.assertRaises(SourceUnavailable):
            ProjectionRelay(LostAck(), self.index).run_once()
        restarted_index = CommunityIndex(self.index_path)
        restarted_index.initialize()
        self.addCleanup(restarted_index.close)
        result = ProjectionRelay(self.source, restarted_index).run_once()
        self.assertEqual(result["duplicate"], 1)
        self.assertEqual(result["acknowledged"], 1)
        self.assertEqual(len(restarted_index.search("durable", limit=10)), 1)

    def test_index_failure_does_not_acknowledge_source_event(self):
        self.publish()

        class FailingIndex:
            def apply(self, event):
                raise OSError("injected index disk failure")

        with self.assertRaises(OSError):
            ProjectionRelay(self.source, FailingIndex()).run_once()
        self.assertEqual(len(self.source.pending("searchforge-content-v1")), 1)
        self.assertEqual(self.relay.run_once()["applied"], 1)

    def test_service_auth_is_distinct_from_user_login_and_consumer_is_scoped(self):
        token, _ = self.publish()
        path = "/internal/v1/content-projection/events?consumer_id=searchforge-content-v1&limit=10"
        self.assertEqual(self.request(self.source_url, "GET", path)[0], 401)
        self.assertEqual(self.request(self.source_url, "GET", path, token=token)[0], 401)
        wrong_consumer = path.replace("searchforge-content-v1", "unapproved-consumer")
        self.assertIn(self.request(self.source_url, "GET", wrong_consumer, token=self.service_token)[0], {400, 403})
        self.assertEqual(self.request(self.source_url, "GET", path, token=self.service_token)[0], 200)

    def test_bad_queries_return_400_and_index_candidates_do_not_bypass_source(self):
        _, result = self.publish()
        self.relay.run_once()
        for path in ("/v1/search", "/v1/search?q=durable&limit=0", "/v1/search?q=a&q=b", "/v1/search?q=*&unknown=true"):
            self.assertEqual(self.request(self.search_url, "GET", path)[0], 400, path)
        actual_source = self.source

        class NoLongerPublic:
            def get_public(self, content_id):
                return None

            def get_public_many(self, content_ids):
                return []

        self.assertEqual(PublicContentSearch(NoLongerPublic(), self.index).search("durable")["items"], [])

        class ObsoleteIndex:
            def get_public(self, content_id):
                value = actual_source.get_public(content_id)
                return {**value, "version": value["version"] + 1}

            def get_public_many(self, content_ids):
                return [{**value, "version": value["version"] + 1} for value in actual_source.get_public_many(content_ids)]

        self.assertEqual(PublicContentSearch(ObsoleteIndex(), self.index).search("durable")["items"], [])
        self.assertIsNone(PublicContentSearch(NoLongerPublic(), self.index).detail(result["content"]["content_id"]))

    def test_source_outage_fails_closed_without_returning_index_body(self):
        self.publish()
        self.relay.run_once()

        class OfflineSource:
            def get_public(self, content_id):
                raise SourceUnavailable("injected source outage")

            def get_public_many(self, content_ids):
                raise SourceUnavailable("injected source outage")

        isolated_url = self.start_server(ContentSearchHTTPServer(("127.0.0.1", 0), PublicContentSearch(OfflineSource(), self.index)))
        status, result = self.request(isolated_url, "GET", "/v1/search?q=durable")
        self.assertEqual(status, 503)
        self.assertEqual(result["error"], "source_unavailable")
        self.assertNotIn("items", result)

    def test_source_configuration_rejects_unprotected_remote_and_credentials(self):
        for url in ("http://example.com", "https://name:password@example.com", "https://example.com/path", "file:///tmp/source"):
            with self.assertRaises(ValueError):
                HTTPProjectionSource(url, "test-token")

    def test_bounded_relay_cli_opens_existing_index_and_closes_resources(self):
        self.publish()
        environment = dict(os.environ)
        environment["C3_TEST_SERVICE_TOKEN"] = self.service_token
        command = [sys.executable, "-B", str(ROOT / "scripts/content.py"), "relay",
                   "--source-url", self.source_url, "--source-token-env", "C3_TEST_SERVICE_TOKEN",
                   "--index", str(self.index_path), "--max-batches", "1"]
        first = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        result = json.loads(first.stdout)
        self.assertEqual(result["batches"][0]["applied"], 1)
        second = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=10)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["batches"][0]["received"], 0)
        self.assertNotIn(self.service_token, first.stdout + first.stderr + second.stdout + second.stderr)

    def test_revalidation_uses_one_bounded_batch_and_rejects_large_pages(self):
        self.publish()
        self.relay.run_once()
        actual_source = self.source
        batches = []

        class CountingSource:
            def get_public_many(self, content_ids):
                batches.append(content_ids)
                return actual_source.get_public_many(content_ids)

            def get_public(self, content_id):
                raise AssertionError("search must not perform per-hit source calls")

        result = PublicContentSearch(CountingSource(), self.index).search("durable", limit=20)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(batches), 1)
        self.assertEqual(self.request(self.search_url, "GET", "/v1/search?q=durable&limit=21")[0], 400)

    def test_search_service_has_bounded_admission(self):
        server = ContentSearchHTTPServer(("127.0.0.1", 0), self.search, max_concurrent_requests=1)
        origin = self.start_server(server)
        self.assertTrue(server._request_slots.acquire(timeout=1))
        try:
            status, result = self.request(origin, "GET", "/healthz")
            self.assertEqual(status, 503, result)
            self.assertEqual(result["error"], "concurrency_limit")
        finally:
            server._request_slots.release()
        self.assertEqual(self.request(origin, "GET", "/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
