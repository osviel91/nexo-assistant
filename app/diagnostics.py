from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

_events: deque[dict[str, Any]] = deque(maxlen=200)


def diagnostic(logger: logging.Logger, event: str, **fields: Any) -> None:
    _events.append({"event": event, "created_at": datetime.now(timezone.utc).isoformat(), **fields})
    logger.info("nexo_diag %s %s", event, json.dumps(fields, separators=(",", ":"), sort_keys=True))


def recent(limit: int = 50) -> list[dict[str, Any]]:
    return list(reversed(_events))[:max(1, min(limit, 200))]
