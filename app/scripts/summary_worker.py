import asyncio
import logging
import os
import socket

from app.services.summary_queue_service import ensure_consumer_group, read_summary_jobs
from app.services.summary_worker_service import process_summary_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _consumer_name() -> str:
    env_name = os.getenv("SUMMARY_WORKER_CONSUMER")
    if env_name:
        return env_name
    return f"{socket.gethostname()}-{os.getpid()}"


async def run_worker():
    consumer_name = _consumer_name()
    ensure_consumer_group()
    logger.info("Summary worker started consumer=%s", consumer_name)

    while True:
        jobs = read_summary_jobs(consumer_name)
        if not jobs:
            await asyncio.sleep(0.2)
            continue

        for stream_id, fields in jobs:
            await process_summary_job(stream_id, fields)


def main():
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
