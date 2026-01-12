"""Helper functions for generating and resolving short callback tokens.

The Telegram inline keyboard callback_data has a length limit; storing client
IDs directly can be long. This module maps longer identifiers to short
tokens using a simple in-memory dictionary with a TTL.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Optional, Tuple, Dict

# Import configuration from settings via absolute import since this module
# is at the top level of the repository.
from settings import CALLBACK_TOKEN_TTL_SEC

__all__ = [
    "register_client_id_for_callback",
    "resolve_client_id_from_callback",
]


# token -> (client_id, created_ts)
_callback_map: Dict[str, Tuple[str, float]] = {}
_callback_lock = asyncio.Lock()


def _cleanup_callback_map(now: float) -> None:
    """Remove expired entries from the callback token map."""
    to_del = [k for k, (_, ts) in _callback_map.items() if now - ts > CALLBACK_TOKEN_TTL_SEC]
    for k in to_del:
        _callback_map.pop(k, None)


async def register_client_id_for_callback(client_id: str) -> str:
    """
    Generate a short token for the given client_id and store it.

    Args:
        client_id: Original client identifier.

    Returns:
        A short token string.
    """
    now = time.time()
    base = f"{client_id}|{now}".encode("utf-8", errors="replace")
    token = hashlib.sha256(base).hexdigest()[:16]
    async with _callback_lock:
        _cleanup_callback_map(now)
        _callback_map[token] = (client_id, now)
    return token


async def resolve_client_id_from_callback(token: str) -> Optional[str]:
    """
    Resolve the original client_id from the given token, if it is still valid.

    Args:
        token: Short token generated earlier.

    Returns:
        The original client_id, or None if token is unknown or expired.
    """
    now = time.time()
    async with _callback_lock:
        _cleanup_callback_map(now)
        item = _callback_map.get(token)
        if not item:
            return None
        client_id, ts = item
        if now - ts > CALLBACK_TOKEN_TTL_SEC:
            _callback_map.pop(token, None)
            return None
        return client_id