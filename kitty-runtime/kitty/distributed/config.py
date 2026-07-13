from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class DistributedSettings:
    database_url: str
    worker_id: str
    poll_interval_seconds: float = 0.25
    lease_seconds: float = 180.0
    concurrency: int = 4
    max_attempts: int = 5
    retry_base_seconds: float = 1.0

    @classmethod
    def from_env(cls, role: str) -> "DistributedSettings":
        database_url = os.getenv("KITTY_DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("KITTY_DATABASE_URL is required for distributed mode")
        identity = os.getenv("KITTY_INSTANCE_ID", "").strip()
        worker_id = identity or f"{role}:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        settings = cls(
            database_url=database_url,
            worker_id=worker_id,
            poll_interval_seconds=float(os.getenv("KITTY_POLL_INTERVAL_SECONDS", "0.25")),
            lease_seconds=float(os.getenv("KITTY_JOB_LEASE_SECONDS", "180")),
            concurrency=int(os.getenv("KITTY_WORKER_CONCURRENCY", "4")),
            max_attempts=int(os.getenv("KITTY_DELIVERY_MAX_ATTEMPTS", "5")),
            retry_base_seconds=float(
                os.getenv("KITTY_DELIVERY_RETRY_BASE_SECONDS", "1")
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("KITTY_POLL_INTERVAL_SECONDS must be positive")
        if self.lease_seconds < 5:
            raise ValueError("KITTY_JOB_LEASE_SECONDS must be at least 5")
        if self.concurrency < 1:
            raise ValueError("KITTY_WORKER_CONCURRENCY must be at least 1")
        if self.max_attempts < 1:
            raise ValueError("KITTY_DELIVERY_MAX_ATTEMPTS must be at least 1")
        if self.retry_base_seconds <= 0:
            raise ValueError("KITTY_DELIVERY_RETRY_BASE_SECONDS must be positive")
