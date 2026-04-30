"""
Time-update Worker
──────────────────
Consumes from queue: device.time_update
Updates raw_time_on_device and time_on_device columns for the non-reboot path.
Mirrors the ?time= handling in feed.router.php#L6311-L6314 for regular heartbeats.
"""
from __future__ import annotations

import json
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
        await channel.set_qos(prefetch_count=100)

        queue = await channel.declare_queue(config.QUEUE_TIME_UPDATE, durable=True)
        logger.info("Time-update worker started")

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process(requeue=True):
                    await _handle(message)


async def _handle(message: aio_pika.abc.AbstractIncomingMessage) -> None:
    try:
        payload = json.loads(message.body)
    except json.JSONDecodeError:
        logger.error("Time-update worker: invalid JSON — discarding")
        return

    device_id = int(payload["device_id"])
    raw_time: str = payload.get("raw_time", "")

    time_on_device: str | None = None
    if raw_time:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                time_on_device = datetime.strptime(raw_time, fmt).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                break
            except ValueError:
                continue

    if time_on_device is None:
        logger.warning(
            "Time-update: could not parse raw_time=%s for device_id=%d",
            raw_time, device_id,
        )
        return

    await db.update_device_time(device_id, raw_time, time_on_device)
    logger.debug("Time-update: device_id=%d updated", device_id)
