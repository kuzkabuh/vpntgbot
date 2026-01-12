"""Simple in-memory pending input state for admin interactions.

This module provides a minimal FSM-like storage to track admin commands that
require additional text input (e.g. user ID, payment details). It uses an
asyncio lock to ensure concurrent access is safe.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Dict

__all__ = [
    "PendingInput",
    "set_pending",
    "pop_pending",
    "peek_pending",
]


@dataclass
class PendingInput:
    action: str
    created_ts: float


# In-memory storage for pending actions keyed by user ID
_pending_by_user: Dict[int, PendingInput] = {}
# Lock to guard access to the pending storage
_pending_lock = asyncio.Lock()
# Time to live for pending actions (seconds)
_PENDING_TTL = 600  # 10 minutes


async def set_pending(user_id: int, action: str) -> None:
    """Record that the user is expected to provide additional input."""
    async with _pending_lock:
        _pending_by_user[user_id] = PendingInput(action=action, created_ts=time.time())


async def pop_pending(user_id: int) -> Optional[PendingInput]:
    """
    Retrieve and remove the pending input for a user.

    If the pending action has expired, it will be removed and None returned.
    """
    now = time.time()
    async with _pending_lock:
        pi = _pending_by_user.get(user_id)
        if not pi:
            return None
        if now - pi.created_ts > _PENDING_TTL:
            _pending_by_user.pop(user_id, None)
            return None
        _pending_by_user.pop(user_id, None)
        return pi


async def peek_pending(user_id: int) -> Optional[PendingInput]:
    """
    Retrieve pending input for a user without removing it.

    If the pending action has expired, it will be removed and None returned.
    """
    now = time.time()
    async with _pending_lock:
        pi = _pending_by_user.get(user_id)
        if not pi:
            return None
        if now - pi.created_ts > _PENDING_TTL:
            _pending_by_user.pop(user_id, None)
            return None
        return pi