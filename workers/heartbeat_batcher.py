"""
Heartbeat Batcher Worker
────────────────────────
Consumes from queue: device.heartbeat
Batches device IDs for HEARTBEAT_BATCH_WINDOW_MS milliseconds (or up to
HEARTBEAT_BATCH_MAX_SIZE messages), then fires ONE bulk UPDATE against PostgreSQL.

Without batching: 10,000 simultaneous reboots → 10,000 DB writes/second.
With batching:    10,000 reboots → ~20 writes/second (10000 / 500 per batch).

Only connectionstatus is updated here (the heartbeat column).
Feed-generation, devicelog writes, and firstconnected are handled by reboot_worker.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import aio_pika

import config
import db

logger = logging.getLogger(__name__)


async def run() -> None:
    connection = await aio_pika.connect_robust(
        config.RABBITMQ_URL,
        reconnect_interval=5,
    )

    async with connection:
        channel = await connection.channel()
        # High prefetch — we accumulate messages ourselves; RabbitMQ should
        # push as many as possible to fill the batch window.
        await channel.set_qos(prefetch_count=config.HEARTBEAT_BATCH_MAX_SIZE * 2)

        queue = await channel.declare_queue(config.QUEUE_HEARTBEAT, durable=True)

        logger.info(
            "Heartbeat batcher started. Window=%dms MaxBatch=%d",
            config.HEARTBEAT_BATCH_WINDOW_MS,
            config.HEARTBEAT_BATCH_MAX_SIZE,
        )

        await _consume_loop(queue)


async def _consume_loop(queue: aio_pika.abc.AbstractQueue) -> None:
    """
    Drain messages for HEARTBEAT_BATCH_WINDOW_MS then flush.
    This is a time-windowed batcher — not an event-per-message processor.
    """
    while True:
        batch: dict[int, aio_pika.abc.AbstractIncomingMessage] = {}
        deadline = (
            asyncio.get_event_loop().time()
            + config.HEARTBEAT_BATCH_WINDOW_MS / 1000.0
        )

        # Accumulate messages until window expires or batch is full
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            if len(batch) >= config.HEARTBEAT_BATCH_MAX_SIZE:
                break

            try:
                msg = await asyncio.wait_for(
                    queue.get(fail=False),      # returns None when queue is empty
                    timeout=max(remaining, 0.05),
                )
            except asyncio.TimeoutError:
                break

            if msg is None:
                # Queue empty — wait a bit and re-check deadline
                await asyncio.sleep(0.05)
                continue

            try:
                import json
                payload = json.loads(msg.body)
                device_id = int(payload["device_id"])
                # Dedup within the batch — last message per device_id wins
                if device_id in batch:
                    await batch[device_id].ack()   # ack the older duplicate
                batch[device_id] = msg
            except Exception:
                logger.exception("Heartbeat batcher: malformed message, nacking")
                await msg.nack(requeue=False)

        if not batch:
            # Nothing arrived this window — short sleep to avoid busy-loop
            await asyncio.sleep(0.1)
            continue

        device_ids = list(batch.keys())
        logger.debug("Heartbeat flush: %d devices", len(device_ids))

        try:
            await db.update_connection_status_bulk(device_ids)
            # ACK all messages after successful DB write
            for msg in batch.values():
                await msg.ack()
        except Exception:
            logger.exception(
                "Heartbeat bulk UPDATE failed for %d devices — nacking all",
                len(device_ids),
            )
            # Nack with requeue so messages are retried
            for msg in batch.values():
                await msg.nack(requeue=True)
            await asyncio.sleep(1)  # back-off before retry
