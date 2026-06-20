# log_config.py
# Single source of truth for logging configuration in the Modal runtime.
# Called from matchit_modal.py, app.py, and set_scheduler.py top-of-file.
# Safe to call multiple times — basicConfig(force=True) replaces any
# prior handlers, so the last caller wins and all callers agree on the
# config.
#
# Why this exists: Modal's `app logs` aggregates stdout only. Python's
# logging defaults to stderr. Without this config, every logger.*
# call is silently dropped. See backlog #5 in the handover for full
# diagnostic.

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """Route Python logging output to stdout so Modal can see it.

    Call once at startup of any process that wants logger output to
    appear in `modal app logs`. Idempotent.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )
