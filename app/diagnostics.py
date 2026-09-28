from __future__ import annotations

import json
import logging
from typing import Any


def diagnostic(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info("nexo_diag %s %s", event, json.dumps(fields, separators=(",", ":"), sort_keys=True))
