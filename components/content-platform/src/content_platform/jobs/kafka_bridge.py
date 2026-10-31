"""Optional at-least-once Kafka transport for the existing SQLite Outbox.

This module uses small ports so delivery and offset rules can be checked with
in-process fakes. The confluent-kafka dependency is loaded only by the runner.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol
from uuid import uuid4

from ..contracts import EventEnvelope
from ..errors import ValidationError
from .consumer import NotificationConsumer
from .outbox import OutboxRepository


class KafkaBridgeError(RuntimeError):
    """A broker send, delivery report, or durable offset commit failed."""


class PoisonEvent(ValueError):
    """A Kafka value cannot be interpreted as a supported event envelope."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecordSender(Protocol):
    def send(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes,
        headers: Sequence[tuple[str, bytes]] | None = None,
    ) -> None: ...


class KafkaProducerPort(Protocol):
    def produce(
        self,
        topic: str,
        *,
        value: bytes,
        key: bytes,
        headers: Sequence[tuple[str, bytes]] | None,
        on_delivery: Callable[[object | None, object], None],
    ) -> None: ...

    def flush(self, timeout: float) -> int: ...


class KafkaMessagePort(Protocol):
    def value(self) -> bytes | str | None: ...
    def key(self) -> bytes | str | None: ...
    def topic(self) -> str: ...
    def partition(self) -> int: ...
    def offset(self) -> int: ...
    def error(self) -> object | None: ...


class KafkaConsumerPort(Protocol):
    def commit(self, *, message: KafkaMessagePort, asynchronous: bool) -> object: ...


@dataclass(frozen=True)
class KafkaEvent:
    event_id: str
    event_type: str
    payload: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class RelayResult:
    claimed: int
    delivered: int
    failed: int


def serialize_event(event: EventEnvelope) -> bytes:
    """Encode the stable transport contract, excluding local lease metadata."""
    if not all(
        isinstance(value, str) and value
        for value in (event.event_id, event.event_type, event.created_at)
    ) or not isinstance(event.payload, dict):
        raise ValidationError("event envelope fields are invalid")
    record = {
        "schema_version": 1,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at,
    }
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def deserialize_event(raw_value: bytes | str | None) -> KafkaEvent:
    """Validate the versioned JSON envelope before invoking a business consumer."""
    if raw_value is None:
        raise PoisonEvent("null_value")
    try:
        decoded = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value
        value = json.loads(decoded, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PoisonEvent("invalid_json") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "event_id", "event_type", "payload", "created_at"
    }:
        raise PoisonEvent("invalid_envelope")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PoisonEvent("unsupported_schema_version")
    if not isinstance(value["event_id"], str) or not 1 <= len(value["event_id"]) <= 200:
        raise PoisonEvent("invalid_event_id")
    if not isinstance(value["event_type"], str) or not 1 <= len(value["event_type"]) <= 100:
        raise PoisonEvent("invalid_event_type")
    if not isinstance(value["created_at"], str) or not value["created_at"]:
        raise PoisonEvent("invalid_created_at")
    if not isinstance(value["payload"], dict):
        raise PoisonEvent("invalid_payload")
    return KafkaEvent(
        event_id=value["event_id"],
        event_type=value["event_type"],
        payload=value["payload"],
        created_at=value["created_at"],
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


class ConfluentKafkaPublisher:
    """Synchronous delivery-report wrapper around a confluent-kafka Producer."""

    def __init__(self, producer: KafkaProducerPort, *, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.producer = producer
        self.timeout_seconds = timeout_seconds

    def send(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes,
        headers: Sequence[tuple[str, bytes]] | None = None,
    ) -> None:
        reports: list[object | None] = []

        def on_delivery(error: object | None, _message: object) -> None:
            reports.append(error)

        self.producer.produce(
            topic,
            value=value,
            key=key,
            headers=headers,
            on_delivery=on_delivery,
        )
        outstanding = self.producer.flush(self.timeout_seconds)
        if outstanding:
            raise KafkaBridgeError("Kafka delivery timed out")
        if len(reports) != 1:
            raise KafkaBridgeError("Kafka delivery report was not received")
        if reports[0] is not None:
            raise KafkaBridgeError("Kafka broker rejected the record")


class KafkaOutboxRelay:
    """Publish leased SQLite events and acknowledge only after broker delivery."""

    def __init__(
        self,
        outbox: OutboxRepository,
        publisher: RecordSender,
        *,
        topic: str,
        worker_id: str | None = None,
        lease_seconds: float = 30.0,
    ) -> None:
        resolved_worker_id = worker_id or f"kafka-relay-{uuid4().hex}"
        if not topic or lease_seconds <= 0:
            raise ValueError("topic and positive lease_seconds are required")
        self.outbox = outbox
        self.publisher = publisher
        self.topic = topic
        self.worker_id = resolved_worker_id
        self.lease_seconds = lease_seconds

    def run_once(self) -> RelayResult:
        # One record per lease keeps the delivery timeout well inside the lease.
        events = self.outbox.claim(
            self.worker_id,
            limit=1,
            lease_seconds=self.lease_seconds,
        )
        delivered = 0
        failed = 0
        for event in events:
            try:
                self.publisher.send(
                    self.topic,
                    serialize_event(event),
                    key=event.event_id.encode("utf-8"),
                )
            except Exception as exc:
                delay = min(2 ** max(0, event.attempts - 1), 60)
                self.outbox.nack(
                    event.event_id,
                    self.worker_id,
                    f"Kafka publish failed: {type(exc).__name__}",
                    retry_delay_seconds=delay,
                )
                failed += 1
            else:
                if self.outbox.ack(event.event_id, self.worker_id):
                    delivered += 1
                else:
                    # The broker accepted the event, but a stale lease cannot be
                    # acknowledged. It may be published again after lease expiry.
                    failed += 1
        return RelayResult(claimed=len(events), delivered=delivered, failed=failed)


class KafkaNotificationProcessor:
    """Apply supported events to SQLite before manually committing Kafka offsets."""

    def __init__(
        self,
        notification_consumer: NotificationConsumer,
        dead_letter_sender: RecordSender,
        *,
        dead_letter_topic: str,
    ) -> None:
        if not dead_letter_topic:
            raise ValueError("dead_letter_topic is required")
        self.notification_consumer = notification_consumer
        self.dead_letter_sender = dead_letter_sender
        self.dead_letter_topic = dead_letter_topic

    def process(self, message: KafkaMessagePort, consumer: KafkaConsumerPort) -> str:
        if message.error() is not None:
            raise KafkaBridgeError("Kafka returned a message-level error")
        raw_value = message.value()
        try:
            event = deserialize_event(raw_value)
            if _key_bytes(message.key()) != event.event_id.encode("utf-8"):
                raise PoisonEvent("event_key_mismatch")
        except PoisonEvent as exc:
            self._dead_letter(message, raw_value, exc.code)
            self._commit_after_processing(message, consumer)
            return "dead_lettered"

        try:
            # NotificationConsumer commits its idempotency receipt and effects in
            # one SQLite transaction. A failed Kafka commit safely replays it.
            self.notification_consumer.consume(event)
        except ValidationError:
            self._dead_letter(message, raw_value, "unsupported_or_invalid_event")
            self._commit_after_processing(message, consumer)
            return "dead_lettered"
        except Exception as exc:
            if not self.notification_consumer.database.is_integrity_error(exc):
                raise
            # A transport event with unknown local references is permanent poison.
            # Preserve the raw record in the DLQ before advancing the source offset.
            self._dead_letter(message, raw_value, "invalid_local_reference")
            self._commit_after_processing(message, consumer)
            return "dead_lettered"
        self._commit_after_processing(message, consumer)
        return "processed"

    def _dead_letter(
        self,
        message: KafkaMessagePort,
        raw_value: bytes | str | None,
        failure_code: str,
    ) -> None:
        topic = message.topic()
        partition = message.partition()
        offset = message.offset()
        identity = f"{topic}:{partition}:{offset}".encode("utf-8")
        dead_letter_key = hashlib.sha256(identity).hexdigest().encode("ascii")
        raw_bytes = raw_value.encode("utf-8") if isinstance(raw_value, str) else (raw_value or b"")
        headers = [
            ("failure_code", failure_code.encode("ascii")),
            ("source_topic", topic.encode("utf-8")),
            ("source_partition", str(partition).encode("ascii")),
            ("source_offset", str(offset).encode("ascii")),
            ("source_value_was_null", b"true" if raw_value is None else b"false"),
        ]
        # A broker-confirmed DLQ write is required before acknowledging poison.
        self.dead_letter_sender.send(
            self.dead_letter_topic,
            raw_bytes,
            key=dead_letter_key,
            headers=headers,
        )

    @staticmethod
    def _commit_after_processing(
        message: KafkaMessagePort, consumer: KafkaConsumerPort
    ) -> None:
        result = consumer.commit(message=message, asynchronous=False)
        if result is None:
            raise KafkaBridgeError("synchronous Kafka offset commit returned no receipt")
        for partition in result:  # type: ignore[union-attr]
            if getattr(partition, "err", None) is not None:
                raise KafkaBridgeError("Kafka offset commit failed for a partition")


def _key_bytes(value: bytes | str | None) -> bytes | None:
    if value is None:
        return None
    return value.encode("utf-8") if isinstance(value, str) else value
