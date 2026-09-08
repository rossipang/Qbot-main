import logging
import os

# Console stays quiet by default; set QBOT_DEBUG=1 for verbose startup/trace.
_DEBUG = os.environ.get("QBOT_DEBUG", "").strip().lower() in ("1", "true", "yes")
_CONSOLE_LEVEL = logging.DEBUG if _DEBUG else logging.WARNING
_LOGGER_LEVEL = logging.DEBUG if _DEBUG else logging.WARNING

LOGGER = logging.getLogger("qbot")
LOGGER.setLevel(_LOGGER_LEVEL)
LOGGER.propagate = False

# Avoid duplicate handlers on re-import / hot reload.
if not getattr(LOGGER, "_qbot_handlers_ready", False):
    ch = logging.StreamHandler()
    ch.setLevel(_CONSOLE_LEVEL)
    fh = logging.FileHandler("qbot_pro.log", encoding="utf-8", mode="a")
    fh.setLevel(logging.WARNING)
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d %(funcName)s: %(message)s"
    )
    ch.setFormatter(formatter)
    fh.setFormatter(formatter)
    LOGGER.addHandler(ch)
    LOGGER.addHandler(fh)
    LOGGER._qbot_handlers_ready = True  # type: ignore[attr-defined]

# Backward-compatible alias used by older call sites.
LOGGER_TXT = LOGGER
