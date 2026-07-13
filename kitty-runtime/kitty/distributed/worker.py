from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from kitty.channels.codec import deserialize_channel_message
from kitty.deployment import DeploymentSettings, build_runtime
from kitty.distributed.config import DistributedSettings
from kitty.memory.postgres_store import InboxJob, LeaseLostError, PostgresStore
from kitty.runtime import KittyRuntime


class AgentWorkerService:
    """Claims durable inbox jobs and writes replies to the durable outbox."""

    def __init__(
        self,
        store: PostgresStore,
        runtime: KittyRuntime,
        settings: DistributedSettings,
    ):
        self.store = store
        self.runtime = runtime
        self.settings = settings
        self.logger = logging.getLogger("kitty.distributed.worker")

    async def run_forever(self) -> None:
        await self.runtime.start()
        tasks = [
            asyncio.create_task(self._slot(index), name=f"kitty-agent-slot:{index}")
            for index in range(self.settings.concurrency)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.runtime.close()
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
            self.store.claim_inbox,
            owner,
            self.settings.lease_seconds,
        )
        if job is None:
            return False
        fencing_token = await asyncio.to_thread(
            self.store.acquire_session_lease,
            job.session_id,
            owner,
            self.settings.lease_seconds,
        )
        if fencing_token is None:
            await asyncio.to_thread(
                self.store.defer_inbox,
                job.job_id,
                owner,
                self.settings.poll_interval_seconds,
            )
            return True

        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(job, owner, fencing_token, lost),
            name=f"kitty-agent-heartbeat:{job.job_id}",
        )
        try:
            reply = await self._execute(job, owner, fencing_token)
            if lost.is_set():
                raise LeaseLostError(f"lease heartbeat failed: {job.job_id}")
            await asyncio.to_thread(
                self.store.complete_inbox_with_outbox,
                job,
                owner,
                fencing_token,
                reply,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.retry_inbox,
                job.job_id,
                owner,
                "worker interrupted",
                0,
            )
            raise
        except LeaseLostError as exc:
            self.logger.warning("agent lease lost job_id=%s error=%s", job.job_id, exc)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= self.settings.max_attempts:
                await asyncio.to_thread(
                    self.store.fail_inbox,
                    job.job_id,
                    owner,
                    error,
                )
                self.logger.error("agent job dead job_id=%s error=%s", job.job_id, error)
            else:
                delay = min(
                    self.settings.retry_base_seconds * (2 ** (job.attempts - 1)),
                    300.0,
                )
                await asyncio.to_thread(
                    self.store.retry_inbox,
                    job.job_id,
                    owner,
                    error,
                    delay,
                )
                self.logger.warning(
                    "agent job retry job_id=%s attempt=%s delay=%.2fs error=%s",
                    job.job_id,
                    job.attempts,
                    delay,
                    error,
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self.runtime.workers.stop(job.session_id)
            await asyncio.to_thread(
                self.store.release_session_lease,
                job.session_id,
                owner,
                fencing_token,
            )
        return True

    async def _execute(self, job: InboxJob, owner: str, fencing_token: int) -> str:
        if job.payload.get("kind") == "reply":
            return str(job.payload.get("reply") or "")
        message = deserialize_channel_message(job.payload)
        with self.store.session_write_lease(job.session_id, owner, fencing_token):
            result = await self.runtime.dispatch(
                message.session_id,
                message.content,
                request_id=message.request_id,
                record=message.record,
            )
        return result.reply

    async def _heartbeat(
        self,
        job: InboxJob,
        owner: str,
        fencing_token: int,
        lost: asyncio.Event,
    ) -> None:
        interval = max(1.0, self.settings.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            inbox_ok, session_ok = await asyncio.gather(
                asyncio.to_thread(
                    self.store.renew_inbox_lease,
                    job.job_id,
                    owner,
                    self.settings.lease_seconds,
                ),
                asyncio.to_thread(
                    self.store.renew_session_lease,
                    job.session_id,
                    owner,
                    fencing_token,
                    self.settings.lease_seconds,
                ),
            )
            if not inbox_ok or not session_ok:
                lost.set()
                return


def create_agent_worker() -> AgentWorkerService:
    deployment = DeploymentSettings.from_env()
    deployment.validate(role="worker")
    distributed = DistributedSettings.from_env("worker")
    store = PostgresStore(
        distributed.database_url,
        max_pool_size=max(4, distributed.concurrency * 3),
    )
    runtime = build_runtime(deployment, store=store, validation_role="worker")
    return AgentWorkerService(store, runtime, distributed)
