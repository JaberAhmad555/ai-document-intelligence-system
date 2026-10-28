from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import socket
from threading import Event
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.database import dispose_database, get_session_factory
from app.workers.jobs import JobStore
from app.workers.runtime import Processor, RunOutcome, WorkerRuntime


logger = logging.getLogger(__name__)


def load_processor(reference: str) -> Processor:
    """Load a trusted operator-configured callable, never a document value."""
    module_name, separator, attribute_name = reference.partition(":")

    if (
        not separator
        or not module_name
        or not attribute_name
        or ":" in attribute_name
    ):
        raise ValueError("Use MODULE:CALLABLE for --processor.")

    module = importlib.import_module(module_name)
    processor = getattr(module, attribute_name)

    if not callable(processor):
        raise ValueError("The configured processor must be callable.")

    return processor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the durable document-processing worker.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one recovery/processing cycle and exit.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="Idle polling interval; default: 2 seconds.",
    )
    parser.add_argument(
        "--worker-id",
        default=(
            f"{socket.gethostname()[:60]}-{os.getpid()}-{uuid4().hex[:8]}"
        ),
        help="Worker identity, up to 100 characters.",
    )
    parser.add_argument(
        "--processor",
        default=None,
        help=(
            "Trusted processor callable as MODULE:CALLABLE. "
            "When omitted, only expired-lease recovery runs."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if not 0.1 <= arguments.poll_seconds <= 300:
        parser.error("--poll-seconds must be between 0.1 and 300.")

    if not arguments.worker_id.strip() or len(arguments.worker_id) > 100:
        parser.error("--worker-id must contain 1 to 100 characters.")

    try:
        settings = get_settings()
    except ValidationError:
        logging.basicConfig(level=logging.ERROR)
        logger.error("Invalid worker configuration. Review backend/.env.")
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        processor = (
            load_processor(arguments.processor)
            if arguments.processor is not None
            else None
        )
    except (ImportError, AttributeError, ValueError):
        logger.error(
            "Cannot load the configured processor. Check MODULE:CALLABLE."
        )
        return 2

    stop = Event()

    def request_shutdown(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        store = JobStore(
            get_session_factory(),
            lease_seconds=settings.job_lease_seconds,
        )
        store.check_database()

        runtime = WorkerRuntime(
            store,
            settings,
            worker_id=arguments.worker_id,
            processor=processor,
        )

        logger.info("Worker started: %s", arguments.worker_id)

        if processor is None:
            logger.warning(
                "No processor configured. Expired leases will be recovered, "
                "but queued documents will not be claimed or completed."
            )

        while not stop.is_set():
            outcome = runtime.run_once()

            if outcome not in {
                RunOutcome.IDLE,
                RunOutcome.WAITING_FOR_PROCESSOR,
            }:
                logger.info("Worker cycle result: %s", outcome.value)

            if arguments.once:
                return 0

            if outcome in {
                RunOutcome.IDLE,
                RunOutcome.WAITING_FOR_PROCESSOR,
            }:
                stop.wait(arguments.poll_seconds)

        logger.info("Worker stopped after its current attempt.")
        return 0

    except SQLAlchemyError as error:
        logger.error(
            "Worker database operation failed (%s). "
            "Check database availability and apply Alembic migrations.",
            type(error).__name__,
        )
        return 1

    except Exception as error:
        logger.error(
            "Worker stopped unexpectedly (%s). "
            "Any active lease will require recovery.",
            type(error).__name__,
        )
        return 1

    finally:
        dispose_database()


if __name__ == "__main__":
    raise SystemExit(main())