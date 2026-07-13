from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress

from kitty.channels.feishu import FeishuSender
from kitty.deployment import DeploymentSettings
from kitty.distributed.config import DistributedSettings
from kitty.memory.postgres_store import OutboxJob, PostgresStore


class SenderService:
    """Claims outbox jobs and performs idempotent Feishu delivery."""

    def __init__(
        self,
        store: PostgresStore,
        sender: FeishuSender,
        settings: DistributedSettings,
    ):
        self.store = store
        self.sender = sender
        self.settings = settings
        self.logger = logging.getLogger("kitty.distributed.sender")

    async def run_forever(self) -> None:
        tasks = [
            asyncio.create_task(self._slot(index), name=f"kitty-sender-slot:{index}")
            for index in range(self.settings.concurrency)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(self.store.close)

    async def _slot(self, index: int) -> None:
        owner = f"{self.settings.worker_id}:{index}"
        while True:
            handled = await self.run_once(owner=owner)
            if not handled:
                await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_once(self, *, owner: str | None = None) -> bool:
        owner = owner or self.settings.worker_id
        job = await asyncio.to_thread(
            self.store.claim_outbox,
            owner,
            self.settings.lease_seconds,
        )
        if job is None:
            return False
        heartbeat = asyncio.create_task(
            self._heartbeat(job, owner),
            name=f"kitty-sender-heartbeat:{job.job_id}",
        )
        try:
            request_uuid = hashlib.sha256(
                f"kitty-outbox:{job.job_id}".encode("utf-8")
            ).hexdigest()[:50]
            await self.sender.send_text(job.chat_id, job.reply_text, request_uuid)
            completed = await asyncio.to_thread(
                self.store.complete_outbox,
                job.job_id,
                owner,
            )
            if not completed:
                self.logger.warning("sender lease lost after delivery job_id=%s", job.job_id)
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.retry_outbox,
                job.job_id,
                owner,
                "sender interrupted",
                0,
            )
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= self.settings.max_attempts:
                await asyncio.to_thread(
                    self.store.fail_outbox,
                    job.job_id,
                    owner,
                    error,
                )
                self.logger.error("outbox job dead job_id=%s error=%s", job.job_id, error)
            else:
                delay = min(
                    self.settings.retry_base_seconds * (2 ** (job.attempts - 1)),
                    300.0,
                )
                await asyncio.to_thread(
                    self.store.retry_outbox,
                    job.job_id,
                    owner,
                    error,
                    delay,
                )
                self.logger.warning(
                    "outbox retry job_id=%s attempt=%s delay=%.2fs error=%s",
                    job.job_id,
                    job.attempts,
                    delay,
                    error,
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _heartbeat(self, job: OutboxJob, owner: str) -> None:
        interval = max(1.0, self.settings.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.store.renew_outbox_lease,
                job.job_id,
                owner,
                self.settings.lease_seconds,
            )
            if not renewed:
                return


def create_sender() -> SenderService:
    deployment = DeploymentSettings.from_env()
    distributed = DistributedSettings.from_env("sender")
    if not deployment.feishu_app_id or not deployment.feishu_app_secret:
        raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET are required for sender")
    store = PostgresStore(
        distributed.database_url,
        max_pool_size=max(4, distributed.concurrency * 2),
    )
    sender = FeishuSender(
        app_id=deployment.feishu_app_id,
        app_secret=deployment.feishu_app_secret,
    )
    return SenderService(store, sender, distributed)
