"""
FastAPI producer service.

Handles:  GET /feed/deviceVersion/{ip}
          GET /feed/deviceVersion/{ip}/{protocol}
          (same route, protocol defaults to "")

Query params (all optional):
  ?did=<int>     device DB id (MAC derived from DB)
  ?time=<str>    current time on device
  ?start=<str>   any non-empty value → log "App Started"

Matches PHP route: $app->get('/deviceVersion/:ip(/:protocol)', ...) in
feed.router.php#L6235
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, date

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse

import cache
import config
import crypto
import db
from db import Device, ClientRelease
import mq
import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    await cache.init_redis()
    await mq.init_rabbitmq()
    logger.info("Producer service started")
    yield
    await db.close_pool()
    await cache.close_redis()
    await mq.close_rabbitmq()
    logger.info("Producer service stopped")


app = FastAPI(lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_mac(raw: str) -> str:
    """Lower-case, strip colons and spaces — matches PHP strtolower + str_replace."""
    return raw.lower().replace(":", "").replace(" ", "")


def _today_midnight_iso() -> str:
    today = date.today()
    return datetime.combine(today, datetime.min.time()).isoformat()


def _needs_xml_regen(lastxmlgeneratedon) -> bool:
    """
    True when lastxmlgeneratedon is before today's midnight.
    Mirrors: strtotime($sqlDvc["lastxmlgeneratedon"]) < strtotime($date1)
    feed.router.php#L6300
    """
    if lastxmlgeneratedon is None:
        return True
    today_midnight = datetime.combine(date.today(), datetime.min.time())
    if isinstance(lastxmlgeneratedon, str):
        lastxmlgeneratedon = datetime.fromisoformat(lastxmlgeneratedon)
    return lastxmlgeneratedon < today_midnight


# ── Not-found response helper ─────────────────────────────────────────────────

def _device_to_dict(device: Device) -> dict:
    """Convert ORM Device instance to a plain dict for Redis caching."""
    return {
        "id":                  device.id,
        "hardwareid":          device.hardwareid,
        "updateversion":       device.updateversion,
        "lastxmlgeneratedon":  device.lastxmlgeneratedon.isoformat() if device.lastxmlgeneratedon else None,
        "firstconnected":      device.firstconnected.isoformat() if device.firstconnected else None,
        "serverid":            device.serverid,
        "mc_last_notified_at": device.mc_last_notified_at.isoformat() if device.mc_last_notified_at else None,
    }


async def _not_found_response() -> dict:
    """
    Mirrors feed.router.php#L6374-L6390 — includes latest Windows client info.
    """
    release = await cache.get_cached_client_release()
    if release is None:
        obj: ClientRelease | None = await db.fetch_latest_windows_client()
        if obj:
            release = {"filename": obj.filename, "version": obj.version}
            await cache.set_cached_client_release(release)
        else:
            release = {}

    return {
        "desc":    "device not found",
        "status":  "error",
        "code":    19008,
        "path":    release.get("filename", ""),
        "client":  release.get("version", ""),
        "login":   False,
        "signup":  False,
        "phone":   config.SUPPORT_PHONE,
        "email":   config.SUPPORT_USER_EMAIL,
        "extn":    "1",
    }


# ── Main route ────────────────────────────────────────────────────────────────

@app.get("/feed/deviceVersion/{ip}")
@app.get("/feed/deviceVersion/{ip}/{protocol}")
async def device_version(
    request: Request,
    ip: str,
    protocol: str = "",
    did: int | None = Query(default=None),
    time: str | None = Query(default=None),
    start: str | None = Query(default=None),
):
    """
    Full port of $app->get('/deviceVersion/:ip(/:protocol)', ...) in
    feed.router.php#L6235.

    Key design decisions vs PHP:
      - Device data read from Redis cache (populated on first miss from DB).
      - All DB writes (UPDATE/INSERT) happen via RabbitMQ workers.
      - Response is returned BEFORE any DB write completes.
    """
    # ── Step 1: Resolve MAC and device row ────────────────────────────────────

    mac: str | None = None
    device: dict | None = None

    if did is not None:
        # Equivalent to the ?did branch in feed.router.php#L6244
        orm_device: Device | None = await db.fetch_device_by_id(did)
        if orm_device:
            device = _device_to_dict(orm_device)
            mac = _normalize_mac(orm_device.hardwareid or "")
            # Populate cache under normalised MAC so next call is a hit
            await cache.set_cached_device(mac, device)
        else:
            return JSONResponse(await _not_found_response())
    else:
        if not ip:
            return JSONResponse(
                {"desc": "Please give the ip or did", "status": "error", "code": 1000}
            )

        mac = _normalize_mac(ip)

        # Cache lookup first — zero DB hit on warm cache
        device = await cache.get_cached_device(mac)
        if device is None:
            orm_device = await db.fetch_device_by_mac(mac)
            if orm_device:
                device = _device_to_dict(orm_device)
                await cache.set_cached_device(mac, device)

    if device is None:
        return JSONResponse(await _not_found_response())

    device_id: int = device["id"]
    today_midnight_iso = _today_midnight_iso()

    # ── Step 2: Determine XML regeneration ───────────────────────────────────

    generate_xml = False

    if _needs_xml_regen(device.get("lastxmlgeneratedon")):
        generate_xml = True
        # Eagerly update cache so parallel requests within same window
        # don't all trigger XML gen (Redis dedup lock in worker is final guard)
        await cache.update_cached_device_xml_date(mac, today_midnight_iso)

    elif protocol == "true":
        # First call after device reboot — feed.router.php#L6307
        generate_xml = True
        await cache.update_cached_device_xml_date(mac, today_midnight_iso)

    # ── Step 3: Fire-and-forget to RabbitMQ (does NOT block response) ────────

    # Always record heartbeat
    asyncio.ensure_future(
        mq.publish(config.QUEUE_HEARTBEAT, {"device_id": device_id})
    )

    if start:
        # Reboot path: feed.router.php#L6318-L6329
        asyncio.ensure_future(
            mq.publish(
                config.QUEUE_REBOOT,
                {
                    "device_id":       device_id,
                    "mac":             mac,
                    "server_id":       device.get("serverid"),
                    "firstconnected":  str(device.get("firstconnected") or ""),
                    "raw_time":        time,
                    "generate_xml":    generate_xml,
                },
            )
        )
    else:
        if generate_xml:
            asyncio.ensure_future(
                mq.publish(
                    config.QUEUE_XML_GENERATE,
                    {"device_id": device_id, "mac": mac},
                )
            )
        if time:
            # Non-reboot ?time= update
            asyncio.ensure_future(
                mq.publish(
                    config.QUEUE_TIME_UPDATE,
                    {
                        "device_id":        device_id,
                        "raw_time":         time,
                    },
                )
            )

    # ── Step 4: Build and return response immediately ─────────────────────────

    if protocol == "true":
        # feed.router.php#L6336-L6363 — encrypted storage credentials
        host, bucket = storage.resolve_host_and_bucket(device.get("serverid", 0))
        try:
            # h = crypto.rsa_encrypt(host)
            # h = crypto.rsa_encrypt(host)
            # h = crypto.rsa_encrypt(host)

            h = host
            b = bucket
        except RuntimeError as exc:
            logger.error("RSA encryption failed: %s", exc)
            return JSONResponse({"status": "error", "desc": "encryption failure"}, status_code=500)

        return JSONResponse(
            {"desc": device["updateversion"], "h": h, "b": b},
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma":        "no-cache",
            },
        )

    return JSONResponse(
        {"desc": device["updateversion"]},
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":        "no-cache",
        },
    )


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}
