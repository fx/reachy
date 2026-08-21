"""Structured logging, and the context that makes a frame findable.

Groundstation REQ-028 asks that every log line emitted while handling a frame
carries the session identifier and the frame's sequence number. Threading both
through every call site would guarantee that one call site eventually forgets,
so they are bound into the context instead: a session binds its identifier for
its whole lifetime, the pipeline binds a sequence number for the span of one
frame, and every line emitted underneath carries both whether or not its author
thought about it.

Asyncio copies the context when it creates a task, so a session's binding
reaches the pipeline task it starts and does not reach any other session's.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final

import structlog

from reachy_groundstation.config import resolved_configuration

if TYPE_CHECKING:
    from collections.abc import Iterator

    from reachy_groundstation.config import Settings

__all__ = [
    "configure_logging",
    "frame_context",
    "get_logger",
    "log_resolved_configuration",
    "session_context",
]

_LEVELS: Final[dict[str, int]] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure_logging(settings: Settings) -> None:
    """Install the process-wide logging configuration.

    Args:
        settings: The settings in effect; its level and format decide what is
            emitted and how.
    """
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            _LEVELS[settings.log_level],
        ),
        logger_factory=structlog.WriteLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Obtain a logger that will carry whatever context is bound.

    Args:
        name: The module asking, conventionally `__name__`.

    Returns:
        The bound logger.
    """
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger


@contextmanager
def session_context(session_id: str) -> Iterator[None]:
    """Bind a session identifier for the lifetime of one session.

    Args:
        session_id: The identifier every line and every exemplar carries.

    Yields:
        Nothing; the binding is the point.
    """
    with structlog.contextvars.bound_contextvars(session=session_id):
        yield


@contextmanager
def frame_context(sequence: int) -> Iterator[None]:
    """Bind a sequence number for the span of one frame.

    Args:
        sequence: The frame's number within its session.

    Yields:
        Nothing; the binding is the point.
    """
    with structlog.contextvars.bound_contextvars(sequence=sequence):
        yield


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
def log_resolved_configuration(settings: Settings) -> None:
    """Emit every setting in effect, including the ones left at their defaults.

    The rendering comes from `resolved_configuration`, which is also what the
    configuration endpoint returns. One renderer means a secret cannot be
    redacted here and reported by value there.

    Args:
        settings: The settings in effect.
    """
    values: dict[str, Any] = dict(resolved_configuration(settings))
    get_logger(__name__).info("configuration.resolved", **values)
