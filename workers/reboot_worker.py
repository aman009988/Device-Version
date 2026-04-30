"""
Reboot Worker
─────────────
Consumes from queue: device.reboot
One message per device reboot (?start=true hit).

Per message:
  1. INSERT INTO devicelog (deviceid, createdon, data) VALUES (?, NOW(), 'App Started')
  2. UPDATE device SET lastxmlgeneratedon, mc_last_notified_at, connectionstatus
     (+ firstconnected if NULL, + time fields if present)
  3. Publish to device.xml_generate if generate_xml == True

Controlled concurrency via prefetch_count (REBOOT_WORKER_PREFETCH, default 50).
At 10k simultaneous reboots, max 50 DB writes run at once; the rest wait in queue.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aio_pika

import config
import db
import mq

logger = logging.getLogger(__name__)


async def run() -> None:
    connection = await aio_pika.connect_robust(
        config.RABBITMQ_URL,
        reconnect_interval=5,
    )
    # RabbitMQ connection for publishing XML-gen messages
    await mq.init_rabbitmq()

    async with connection:
        channel = await connection.channel()
        # prefetch_count controls max concurrent in-flight messages per worker process
        await channel.set_qos(prefetch_count=config.REBOOT_WORKER_PREFETCH)

        queue = await channel.declare_queue(config.QUEUE_REBOOT, durable=True)

        logger.info(
            "Reboot worker started. prefetch_count=%d",
            config.REBOOT_WORKER_PREFETCH,
        )

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process(requeue=True):
                    await _handle(message)


async def _handle(message: aio_pika.abc.AbstractIncomingMessage) -> None:
    try:
        payload = json.loads(message.body)
    except json.JSONDecodeError:
        logger.error("Reboot worker: invalid JSON — discarding message")
        # process() context manager will ack; we already nacked logically.
        return

    device_id    = int(payload["device_id"])
    mac: str     = payload.get("mac", "")
    firstconn    = payload.get("firstconnected", "")   # empty string means NULL in DB
    raw_time     = payload.get("raw_time")             # None if not provided
    generate_xml = bool(payload.get("generate_xml", False))

    set_firstconnected = not bool(firstconn)  # True when firstconnected was NULL/empty

    logger.debug(
        "Reboot device_id=%d mac=%s gen_xml=%s firstconn=%s",
        device_id, mac, generate_xml, firstconn,
    )

    # Compute time_on_device if raw_time was passed
    time_on_device: str | None = None
    if raw_time:
        try:
            from datetime import datetime
            # Mirrors PHP: date('Y-m-d H:i:s', strtotime($_GET['time']))
            # Try ISO format first, fall back to strptime common formats
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    time_on_device = datetime.strptime(raw_time, fmt).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    break
                except ValueError:
                    continue
        except Exception:
            logger.warning("Could not parse raw_time=%s", raw_time)

    # 1. Log "App Started" — feed.router.php#L6318
    await db.insert_app_started_log(device_id)

    # 2. Update device record — feed.router.php#L6296-L6316
    await db.update_device_on_reboot(
        device_id=device_id,
        set_firstconnected=set_firstconnected,
        raw_time=raw_time,
        time_on_device=time_on_device,
    )

    # 3. Trigger XML generation if needed — feed.router.php#L6367
    if generate_xml:
        await mq.publish(
            config.QUEUE_XML_GENERATE,
            {"device_id": device_id, "mac": mac},
        )

    logger.debug("Reboot worker: completed device_id=%d", device_id)
