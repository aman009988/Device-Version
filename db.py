"""
Database layer — SQLAlchemy ORM (async, declarative).

Models map directly to the existing PostgreSQL tables.
Queries use ORM session.get(), session.execute(select(Model)), and
session.execute(update(Model)) — no raw SQL strings anywhere.
asyncpg remains the underlying driver via create_async_engine.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, date
from typing import AsyncGenerator

from sqlalchemy import Boolean, DateTime, Integer, String, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config


# ── Declarative base ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── ORM Models ────────────────────────────────────────────────────────────────
# Only columns this service reads/writes are declared.
# Extra columns in the real table are ignored by SQLAlchemy automatically.

class Device(Base):
    """Maps to public.device — feed.router.php fetches this table on every call."""

    __tablename__ = "device"

    id:                  Mapped[int]            = mapped_column(Integer, primary_key=True)
    hardwareid:          Mapped[str | None]      = mapped_column(String)
    updateversion:       Mapped[str | None]      = mapped_column(String)
    lastxmlgeneratedon:  Mapped[datetime | None] = mapped_column(DateTime)
    firstconnected:      Mapped[datetime | None] = mapped_column(DateTime)
    connectionstatus:    Mapped[datetime | None] = mapped_column(DateTime)
    serverid:            Mapped[int | None]      = mapped_column(Integer)
    mc_last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    isactive:            Mapped[bool | None]     = mapped_column(Boolean)
    raw_time_on_device:  Mapped[str | None]      = mapped_column(String)
    time_on_device:      Mapped[datetime | None] = mapped_column(DateTime)


class DeviceLog(Base):
    """Maps to public.devicelog — one row inserted per device reboot."""

    __tablename__ = "devicelog"

    id:        Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    deviceid:  Mapped[int]          = mapped_column(Integer)
    createdon: Mapped[datetime]     = mapped_column(DateTime)
    data:      Mapped[str]          = mapped_column(String)


class ClientRelease(Base):
    """Maps to hubcp.clientreleases — queried when a device is not found."""

    __tablename__  = "clientreleases"
    __table_args__ = {"schema": "hubcp"}

    id:       Mapped[int]      = mapped_column(Integer, primary_key=True)
    filename: Mapped[str]      = mapped_column(String)
    version:  Mapped[str]      = mapped_column(String)
    platform: Mapped[str]      = mapped_column(String)
    status:   Mapped[str]      = mapped_column(String)


# ── Engine & session factory ──────────────────────────────────────────────────

_engine:          AsyncEngine        | None = None
_session_factory: async_sessionmaker | None = None


def _build_url() -> str:
    return (
        f"postgresql+asyncpg://{config.DB_USER}:{config.DB_PASSWORD}"
        f"@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}"
    )


async def init_pool() -> None:
    global _engine, _session_factory
    _engine = create_async_engine(
        _build_url(),
        pool_size=config.DB_POOL_MIN,
        max_overflow=config.DB_POOL_MAX - config.DB_POOL_MIN,
        pool_timeout=10,
        pool_pre_ping=True,
        echo=False,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def close_pool() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yields a new AsyncSession; commits on success, rolls back on exception."""
    if _session_factory is None:
        raise RuntimeError("DB not initialised — call init_pool() first")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Helper ────────────────────────────────────────────────────────────────────

def _today_midnight() -> datetime:
    return datetime.combine(date.today(), datetime.min.time())


# ── Read queries ──────────────────────────────────────────────────────────────

async def fetch_device_by_mac(mac: str) -> Device | None:
    """
    ORM equivalent of feed.router.php#L6266.
    mac must already be normalised (lower, no colons/spaces).
    func.replace + func.lower produces a bound-parameter expression — no interpolation.
    """
    normalised = func.replace(func.lower(Device.hardwareid), ":", "")
    stmt = (
        select(Device)
        .where(normalised == mac)
        .where(Device.isactive == 1)   # noqa: E712
        .limit(1)
    )
    async with get_session() as session:
        result = await session.execute(stmt)
        return result.scalars().first()


async def fetch_device_by_id(device_id: int) -> Device | None:
    """
    ORM equivalent of getDeviceInfo($app, $did, ...) — feed.router.php#L22197.
    Uses session.get() for primary-key lookup — most efficient ORM path.
    """
    async with get_session() as session:
        device = await session.get(Device, device_id)
        if device and device.isactive:
            return device
        return None


async def fetch_latest_windows_client() -> ClientRelease | None:
    """
    ORM equivalent of getOne("hubcp.clientreleases", ...) — feed.router.php#L6380.
    """
    stmt = (
        select(ClientRelease)
        .where(ClientRelease.platform == "windows")
        .where(ClientRelease.status == "active")
        .order_by(ClientRelease.id.desc())
        .limit(1)
    )
    async with get_session() as session:
        result = await session.execute(stmt)
        return result.scalars().first()


# ── Write queries (called by workers only — never by the API handler) ─────────

async def update_connection_status_bulk(device_ids: list[int]) -> None:
    """
    Heartbeat batcher: one bulk UPDATE replaces N individual writes.
    ORM bulk update via synchronize_session=False — does not load objects into memory.
    Equivalent to: UPDATE device SET connectionstatus = NOW() WHERE id IN (...)
    """
    stmt = (
        update(Device)
        .where(Device.id.in_(device_ids))
        .values(connectionstatus=func.now())
        .execution_options(synchronize_session=False)
    )
    async with get_session() as session:
        await session.execute(stmt)


async def update_device_on_reboot(
    device_id: int,
    set_firstconnected: bool,
    raw_time: str | None,
    time_on_device: str | None,
) -> None:
    """
    ORM equivalent of the UPDATE block in feed.router.php#L6296-L6316.
    Values dict is built conditionally; single ORM update statement is executed.
    """
    values: dict = {
        "lastxmlgeneratedon":  _today_midnight(),
        "mc_last_notified_at": None,
        "connectionstatus":    func.now(),
    }
    if set_firstconnected:
        values["firstconnected"] = func.now()
    if raw_time is not None:
        values["raw_time_on_device"] = raw_time
    if time_on_device is not None:
        values["time_on_device"] = time_on_device

    stmt = (
        update(Device)
        .where(Device.id == device_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    async with get_session() as session:
        await session.execute(stmt)


async def update_device_time(
    device_id: int, raw_time: str, time_on_device: str
) -> None:
    """
    Update only the time-on-device fields for the non-reboot ?time= path.
    feed.router.php#L6311-L6314
    """
    stmt = (
        update(Device)
        .where(Device.id == device_id)
        .values(raw_time_on_device=raw_time, time_on_device=time_on_device)
        .execution_options(synchronize_session=False)
    )
    async with get_session() as session:
        await session.execute(stmt)


async def insert_app_started_log(device_id: int) -> None:
    """
    ORM equivalent of INSERT INTO devicelog — feed.router.php#L6318-L6321.
    Adds one DeviceLog row; session.add() + commit handled by get_session().
    """
    log_entry = DeviceLog(
        deviceid=device_id,
        createdon=datetime.utcnow(),
        data="App Started",
    )
    async with get_session() as session:
        session.add(log_entry)
