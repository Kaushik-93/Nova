"""Data values, column names and comments can contain anything, including text
written to manipulate an AI. They are shown to the model only inside these markers,
and the system prompts say that nothing inside them is ever an instruction.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

OPEN = "<untrusted_data>"
CLOSE = "</untrusted_data>"
_MARKER = re.compile(r"<\s*/?\s*untrusted_data\s*>", re.IGNORECASE)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def wrap_untrusted(value: Any) -> str:
    """Serialise `value` for the model. Marker text inside the data is neutralised,
    so a value can't close the block early and pose as instructions."""
    text = value if isinstance(value, str) else json.dumps(value, default=_jsonable, ensure_ascii=False)
    return f"{OPEN}\n{_MARKER.sub('[marker removed]', text)}\n{CLOSE}"
