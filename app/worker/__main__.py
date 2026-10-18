"""Worker process entry point: runs the scheduler until SIGTERM or SIGINT."""

import logging
import signal
import threading
from types import FrameType

from app.config import get_settings
from app.logging_config import configure_logging
from app.worker.runtime import build_scheduler


def main() -> None:
    """Start the scheduler and block until a shutdown signal, then stop it gracefully."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    shutdown = threading.Event()

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        """Log the signal and release the main thread so it can shut down."""
        del frame
        logger.info("Worker received shutdown signal", extra={"signal": signum})
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    scheduler = build_scheduler(settings)
    scheduler.start()
    logger.info("Worker started; jobs are read from the metadata database")
    shutdown.wait()
    scheduler.stop(wait=True)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
