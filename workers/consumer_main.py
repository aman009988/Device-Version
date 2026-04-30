"""
Consumer service entrypoint.
Runs all workers concurrently in a single asyncio event loop.
Each worker is an independent coroutine; if one crashes it is restarted.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import os

# Allow imports from the parent package (same directory level as main.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import cache
import mq

from workers import heartbeat_batcher, reboot_worker, xml_gen_worker, time_update_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


async def run_with_restart(name: str, coro_factory) -> None:
    """
    Run a worker coroutine and restart it on failure with exponential back-off.
    Max back-off = 30 seconds.
    """
    backoff = 1
    while True:
        try:
            logger.info("Starting worker: %s", name)
            await coro_factory()
            logger.warning("Worker %s exited cleanly — restarting", name)
        except Exception:
            logger.exception("Worker %s crashed — restarting in %ds", name, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        else:
            backoff = 1  # reset on clean exit


async def main() -> None:
    # Shared infrastructure initialised once
    await db.init_pool()
    await cache.init_redis()
    await mq.init_rabbitmq()

    logger.info("Consumer service started — launching all workers")

    await asyncio.gather(
        run_with_restart("heartbeat_batcher",   heartbeat_batcher.run),
        run_with_restart("reboot_worker",        reboot_worker.run),
        run_with_restart("xml_gen_worker",       xml_gen_worker.run),
        run_with_restart("time_update_worker",   time_update_worker.run),
    )


if __name__ == "__main__":
    asyncio.run(main())
