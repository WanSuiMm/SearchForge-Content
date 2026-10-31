"""Small read-only HTTP API for the explicit single-node content vertical."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .runtime import PublicContentSearch
from .source import SourceUnavailable


class ContentSearchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], search: PublicContentSearch, *, max_concurrent_requests: int = 8) -> None:
        if type(max_concurrent_requests) is not int or not 1 <= max_concurrent_requests <= 64:
            raise ValueError("request concurrency must be an integer from 1 to 64")
        self.search = search
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        super().__init__(address, _Handler)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            # Drain a small GET header before closing so the client can receive
            # the explicit rejection instead of a reset from unread TCP data.
            try:
                request.settimeout(0.1)
                request.recv(8192)
            except OSError:
                pass
            body = b'{"error":"concurrency_limit"}'
            response = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                        b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            try:
                request.sendall(response)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class _Handler(BaseHTTPRequestHandler):
    server: ContentSearchHTTPServer

    def setup(self):
        super().setup()
        self.connection.settimeout(5.0)

    def log_message(self, format, *args):
        # Avoid persisting user queries and content identifiers in access logs.
        return

    def do_GET(self):
        try:
            if len(self.path) > 2048:
                self._send(414, {"error": "request_target_too_long"})
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/healthz" and not query:
                self._send(200, {"status": "alive", "scope": "process_only"})
                return
            if parsed.path == "/v1/search":
                if set(query) - {"q", "limit"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("unsupported or repeated query parameter")
                if "q" not in query:
                    raise ValueError("q is required")
                result = self.server.search.search(query["q"][0], limit=int(query.get("limit", ["20"])[0]))
                self._send(200, result)
                return
            prefix = "/v1/content/"
            if parsed.path.startswith(prefix) and not query:
                identifier = unquote(parsed.path[len(prefix):])
                if not identifier or "/" in identifier or len(identifier) > 200:
                    raise ValueError("content identifier is invalid")
                result = self.server.search.detail(identifier)
                self._send(200 if result else 404, result or {"error": "not_found"})
                return
            self._send(404, {"error": "not_found"})
        except ValueError as exc:
            self._send(400, {"error": "invalid_request", "message": str(exc)})
        except SourceUnavailable:
            self._send(503, {"error": "source_unavailable", "message": "Content source verification is unavailable"})
        except Exception:
            self._send(503, {"error": "index_unavailable", "message": "Content search is unavailable"})

    def _send(self, status: int, value: dict):
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
