"""
XML Generate Worker
───────────────────
Consumes from queue: device.xml_generate

Per message:
  1. Acquire Redis dedup lock (xmlgen:lock:{device_id}, TTL=XML_GEN_LOCK_TTL seconds).
     If lock already held → ACK and skip (another worker already handling this device).
  2. POST to QUEUE_SERVER (existing PHP service) OR call PHP getfeeds3 route directly.
     Mirrors generateXmlForDevice() in customFunctions.php#L12852.

This worker intentionally runs with low concurrency (prefetch_count=10) because
calling PHP getfeeds3 is CPU/DB heavy. Thundering-herd absorption happens upstream
in the RabbitMQ queue.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aio_pika
import httpx

import cache
import config

logger = logging.getLogger(__name__)

_XML_GEN_PREFETCH = 10          # intentionally low — honour DB capacity


async def run() -> None:
    connection = await aio_pika.connect_robust(
        config.RABBITMQ_URL,
        reconnect_interval=5,
    )
    await cache.init_redis()

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=_XML_GEN_PREFETCH)

        queue = await channel.declare_queue(config.QUEUE_XML_GENERATE, durable=True)

        logger.info("XML generate worker started. prefetch=%d", _XML_GEN_PREFETCH)

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process(requeue=True):
                    await _handle(message)


async def _handle(message: aio_pika.abc.AbstractIncomingMessage) -> None:
    try:
        payload = json.loads(message.body)
    except json.JSONDecodeError:
        logger.error("XML gen worker: invalid JSON — discarding")
        return

    device_id = int(payload["device_id"])
    mac: str  = payload.get("mac", "")

    # ── Redis dedup lock ──────────────────────────────────────────────────────
    # Mirrors the PHP logic where generateXmlForDevice is called at most once
    # per day per device (within the same PHP request cycle).
    # Lock TTL = XML_GEN_LOCK_TTL seconds (default 60).
    acquired = await cache.acquire_xmlgen_lock(device_id)
    if not acquired:
        logger.debug(
            "XML gen: skipping device_id=%d mac=%s — lock already held",
            device_id, mac,
        )
        return  # message.process() will ACK

    logger.info("XML gen: triggering device_id=%d mac=%s", device_id, mac)

    if config.PHP_QUEUE_SERVER:
        await _call_php_queue_server(mac)
    elif config.PHP_APIPATH:
        await _call_php_getfeeds3(mac)
    else:
        logger.error(
            "XML gen: neither PHP_QUEUE_SERVER nor PHP_APIPATH configured "
            "— cannot trigger XML generation for device_id=%d",
            device_id,
        )


async def _call_php_queue_server(mac: str) -> None:
    """
    POST to QUEUE_SERVER/single with {"mac": [mac], "callback_url": "...getfeeds3"}.
    Mirrors sendDevicesInQueue() in customFunctions.php#L13006.
    """
    url = f"{config.PHP_QUEUE_SERVER.rstrip('/')}/single"
    payload = {
        "mac":          [mac],
        "callback_url": f"{config.PHP_APIPATH.rstrip('/')}/getfeeds3",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.debug("XML gen: queue server responded %d for mac=%s", resp.status_code, mac)
    except Exception:
        logger.exception("XML gen: failed to call queue server for mac=%s", mac)
        raise   # re-raise so message.process() nacks and requeues


async def _call_php_getfeeds3(mac: str) -> None:
    """
    Fallback: call PHP getfeeds3 route directly (no queue server).
    Mirrors the `else` branch in generateXmlForDevice() which calls
    getResponseOfNamedRoute("getfeeds3", ...).
    Uses --no-check-certificate equivalent (verify=False) to match PHP wget behaviour.
    """
    url = f"{config.PHP_APIPATH.rstrip('/')}/getfeeds3/{mac}"
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            logger.debug("XML gen: getfeeds3 responded %d for mac=%s", resp.status_code, mac)
    except Exception:
        logger.exception("XML gen: failed to call getfeeds3 for mac=%s", mac)
        raise
