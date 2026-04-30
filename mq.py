"""
RabbitMQ publisher — used by the FastAPI producer service only.
Each publish is fire-and-forget (non-blocking from the request handler's POV).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, Message

import config

logger = logging.getLogger(__name__)

_connection: aio_pika.RobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None


async def init_rabbitmq() -> None:
    global _connection, _channel
    _connection = await aio_pika.connect_robust(
        config.RABBITMQ_URL,
        reconnect_interval=5,
    )
    _channel = await _connection.channel()

    # Declare all queues as durable so messages survive broker restart
    for queue_name in (
        config.QUEUE_HEARTBEAT,
        config.QUEUE_REBOOT,
        config.QUEUE_XML_GENERATE,
        config.QUEUE_TIME_UPDATE,
    ):
        await _channel.declare_queue(queue_name, durable=True)

    logger.info("RabbitMQ publisher ready")


async def close_rabbitmq() -> None:
    global _connection, _channel
    if _connection:
        await _connection.close()
        _connection = None
        _channel = None


async def publish(queue_name: str, payload: dict[str, Any]) -> None:
    """
    Publish a JSON message to `queue_name`.
    DeliveryMode.PERSISTENT ensures messages survive broker restart.
    Errors are logged but do NOT propagate to the HTTP response — the device
    already got its answer; the write is best-effort async.
    """
    if _channel is None:
        logger.error("RabbitMQ channel not ready; dropping message to %s", queue_name)
        return
    try:
        await _channel.default_exchange.publish(
            Message(
                body=json.dumps(payload, default=str).encode(),
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
            ),
            routing_key=queue_name,
        )
    except Exception:
        logger.exception("Failed to publish to queue=%s payload=%s", queue_name, payload)
