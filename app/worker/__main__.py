import logging
import signal
import threading
from types import FrameType

from app.config import get_settings
from app.logging_config import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    shutdown = threading.Event()

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        del frame
        logger.info("Worker placeholder received shutdown signal", extra={"signal": signum})
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    logger.info("Worker placeholder started; no jobs are registered yet")
    shutdown.wait()


if __name__ == "__main__":
    main()
