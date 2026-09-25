from __future__ import annotations

import json
import logging
from typing import Any


logger = logging.getLogger("hoops_forward.payments")
logger.setLevel(logging.INFO)


def log_payment_event(event: str, **fields: Any) -> None:
    safe_fields = {key: value for key, value in fields.items() if value is not None}
    logger.info(json.dumps({"event": event, **safe_fields}, sort_keys=True, default=str))
