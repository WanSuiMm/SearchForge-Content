"""Dedicated HTTP boundary to the content source; no shared database access."""
from __future__ import annotations

import json
import math
import re
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SourceUnavailable(RuntimeError):
    """A source operation failed or returned a response outside its contract."""


class ProjectionSource(Protocol):
    def pending(self, consumer_id: str, *, limit: int = 100) -> list[dict]: ...
    def acknowledge(self, consumer_id: str, event_id: str) -> bool: ...
    def get_public(self, content_id: str) -> dict | None: ...
    def get_public_many(self, content_ids: list[str]) -> list[dict]: ...


def validate_consumer(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", value):
        raise ValueError("consumer_id must be a stable identifier of 1 to 100 characters")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A dedicated service token must never follow a redirect to another origin.
        return None


class HTTPProjectionSource:
    """Strict, bounded source client with explicit service credentials.

    Plain HTTP is accepted only on loopback. Production remote sources require
    HTTPS; redirects and proxy-environment credential forwarding are disabled.
    """

    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 5.0) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("source URL must be an HTTP(S) origin without credentials or a path")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("non-loopback source connections require HTTPS")
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise ValueError("source service token is required and must not contain whitespace")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60):
            raise ValueError("source timeout must be from 0 to 60 seconds, exclusive of 0")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout_seconds = timeout_seconds
        from urllib.request import ProxyHandler

        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, path: str, *, body: dict | None = None, allow_missing: bool = False) -> dict | None:
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = Request(self.base_url + path, data=encoded, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json", "Content-Type": "application/json",
        }, method="GET" if body is None else "POST")
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise SourceUnavailable("source response exceeded the byte limit")
                if response.headers.get_content_type() != "application/json":
                    raise SourceUnavailable("source returned an unexpected content type")
        except HTTPError as exc:
            if allow_missing and exc.code == 404:
                return None
            raise SourceUnavailable(f"source returned HTTP {exc.code}") from None
        except (URLError, OSError, TimeoutError) as exc:
            # Neither the request representation nor its bearer token enters logs.
            raise SourceUnavailable("source could not be reached") from None
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            raise SourceUnavailable("source returned invalid JSON") from None
        if not isinstance(value, dict):
            raise SourceUnavailable("source response must be a JSON object")
        return value

    def pending(self, consumer_id: str, *, limit: int = 100) -> list[dict]:
        validate_consumer(consumer_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("batch limit must be an integer from 1 to 100")
        query = urlencode({"consumer_id": consumer_id, "limit": limit})
        result = self._request("/internal/v1/content-projection/events?" + query)
        events = result.get("events")
        if not isinstance(events, list) or len(events) > limit or any(not isinstance(e, dict) for e in events):
            raise SourceUnavailable("source returned an invalid event batch")
        return events

    def acknowledge(self, consumer_id: str, event_id: str) -> bool:
        validate_consumer(consumer_id)
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise ValueError("event_id is invalid")
        result = self._request("/internal/v1/content-projection/ack", body={
            "consumer_id": consumer_id, "event_id": event_id,
        })
        if not isinstance(result.get("acknowledged"), bool):
            raise SourceUnavailable("source returned an invalid acknowledgement")
        return result["acknowledged"]

    def get_public(self, content_id: str) -> dict | None:
        if not isinstance(content_id, str) or not content_id or len(content_id) > 200:
            raise ValueError("content_id is invalid")
        return self._request("/internal/v1/content-projection/content/" + quote(content_id, safe=""), allow_missing=True)

    def get_public_many(self, content_ids: list[str]) -> list[dict]:
        if (not isinstance(content_ids, list) or len(content_ids) > 20
                or any(not isinstance(value, str) or not value or len(value) > 200 for value in content_ids)
                or len(set(content_ids)) != len(content_ids)):
            raise ValueError("content resolution requires at most 20 distinct identifiers")
        if not content_ids:
            return []
        result = self._request("/internal/v1/content-projection/resolve", body={"content_ids": content_ids})
        items = result.get("items")
        if not isinstance(items, list) or len(items) > len(content_ids):
            raise SourceUnavailable("source returned an invalid content batch")
        seen = set()
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("content_id"), str)
                    or item["content_id"] not in content_ids or item["content_id"] in seen):
                raise SourceUnavailable("source returned inconsistent content identifiers")
            seen.add(item["content_id"])
        return items
