"""
Redis cache helpers.

Key schema
──────────
device:mac:{normalized_mac}   → JSON dict of device row          TTL = DEVICE_CACHE_TTL
client:release:windows        → JSON dict of latest release row  TTL = CLIENT_RELEASE_CACHE_TTL
xmlgen:lock:{device_id}       → "1" (SET NX)                     TTL = XML_GEN_LOCK_TTL
"""
from __future__ import annotations

import json
import logging

import redis.asyncio as aioredis

import config

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


async def init_redis() -> None:
    global _redis
    _redis = aioredis.from_url(
        config.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        retry_on_timeout=True,
    )


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


def _r() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised")
    return _redis


# ── Device cache ──────────────────────────────────────────────────────────────

def _device_key(mac: str) -> str:
    return f"device:mac:{mac}"


async def get_cached_device(mac: str) -> dict | None:
    try:
        raw = await _r().get(_device_key(mac))
        if raw:
            return json.loads(raw)
    except Exception:
        logger.exception("Redis get_cached_device failed for mac=%s", mac)
    return None


async def set_cached_device(mac: str, device: dict) -> None:
    try:
        await _r().setex(
            _device_key(mac),
            config.DEVICE_CACHE_TTL,
            json.dumps(device, default=str),
        )
    except Exception:
        logger.exception("Redis set_cached_device failed for mac=%s", mac)


async def update_cached_device_xml_date(mac: str, today_midnight_iso: str) -> None:
    """
    Eagerly update the cached lastxmlgeneratedon so that parallel requests
    within the same 500 ms window do NOT all trigger XML generation.
    This is a best-effort write; failures are safe (worst case: duplicate
    XML gen is deduplicated by the xmlgen:lock in Redis).
    """
    try:
        raw = await _r().get(_device_key(mac))
        if raw:
            device = json.loads(raw)
            device["lastxmlgeneratedon"] = today_midnight_iso
            device["mc_last_notified_at"] = None
            await _r().setex(
                _device_key(mac),
                config.DEVICE_CACHE_TTL,
                json.dumps(device, default=str),
            )
    except Exception:
        logger.exception("Redis update_cached_device_xml_date failed mac=%s", mac)


async def invalidate_device_cache(mac: str) -> None:
    try:
        await _r().delete(_device_key(mac))
    except Exception:
        logger.exception("Redis invalidate_device_cache failed mac=%s", mac)


# ── Client release cache ──────────────────────────────────────────────────────

_CLIENT_RELEASE_KEY = "client:release:windows"


async def get_cached_client_release() -> dict | None:
    try:
        raw = await _r().get(_CLIENT_RELEASE_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        logger.exception("Redis get_cached_client_release failed")
    return None


async def set_cached_client_release(release: dict) -> None:
    try:
        await _r().setex(
            _CLIENT_RELEASE_KEY,
            config.CLIENT_RELEASE_CACHE_TTL,
            json.dumps(release, default=str),
        )
    except Exception:
        logger.exception("Redis set_cached_client_release failed")


# ── XML generation dedup lock ─────────────────────────────────────────────────

def _xmlgen_lock_key(device_id: int) -> str:
    return f"xmlgen:lock:{device_id}"


async def acquire_xmlgen_lock(device_id: int) -> bool:
    """
    Returns True if the lock was acquired (caller should proceed).
    Returns False if the lock already exists (skip — already being processed).
    Uses SET NX EX which is atomic; safe for concurrent workers.
    """
    try:
        result = await _r().set(
            _xmlgen_lock_key(device_id),
            "1",
            nx=True,
            ex=config.XML_GEN_LOCK_TTL,
        )
        return result is not None
    except Exception:
        logger.exception("Redis acquire_xmlgen_lock failed device_id=%s", device_id)
        # On Redis failure: allow through (don't block real work)
        return True
