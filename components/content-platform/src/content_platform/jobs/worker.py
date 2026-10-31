"""Bounded polling worker for the transactional Outbox."""

from __future__ import annotations

import threading
from uuid import uuid4

from .consumer import NotificationConsumer
from .outbox import OutboxRepository


class OutboxWorker:
    def __init__(
        self,
        outbox: OutboxRepository,
        consumer: NotificationConsumer,
        *,
        poll_interval_seconds: float = 0.1,
        lease_seconds: float = 30.0,
        batch_size: int = 10,
        worker_id: str | None = None,
    ) -> None:
        if poll_interval_seconds <= 0 or lease_seconds <= 0:
            raise ValueError("worker intervals must be positive")
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be from 1 to 100")
        self.outbox = outbox
        self.consumer = consumer
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.batch_size = batch_size
        self.worker_id = worker_id or f"local-outbox-{uuid4().hex[:16]}"
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.processed_count = 0
        self.failed_count = 0

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"content-platform-{self.worker_id}",
                daemon=False,
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
            if thread.is_alive():
                raise TimeoutError("outbox worker did not stop before the deadline")

    def run_once(self) -> int:
        events = self.outbox.claim(
            self.worker_id,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        completed = 0
        for event in events:
            try:
                self.consumer.consume(event)
                self.outbox.ack(event.event_id, self.worker_id)
                self.processed_count += 1
                completed += 1
            except Exception:
                # Keep error text independent of payloads, credentials and content.
                self.failed_count += 1
                self.outbox.nack(event.event_id, self.worker_id, "notification consumer failed")
        return completed

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:
                self.failed_count += 1
                processed = 0
            if processed == 0:
                self._stop_event.wait(self.poll_interval_seconds)
