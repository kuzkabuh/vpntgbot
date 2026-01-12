"""HTTP client for the backend service.

Provides a helper to call backend endpoints with proper error handling.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

# Import configuration from the settings module. Use absolute import
# because this file is at the package root rather than in a package.
from settings import BACKEND_BASE_URL, BACKEND_TIMEOUT, BACKEND_CONNECT_TIMEOUT

logger = logging.getLogger("vpn-bot.backend")


class BackendError(RuntimeError):
    """Exception raised when backend returns an error."""
    pass


def _extract_backend_detail(payload: Any, status_code: int) -> str:
    """Extract error detail from backend JSON response."""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return f"Ошибка backend (HTTP {status_code})"


async def call_backend(
    *,
    method: str,
    path: str,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> dict:
    """
    Call a backend API endpoint and return the parsed JSON response.

    Args:
        method: HTTP method ("GET", "POST", etc.).
        path: Endpoint path starting with "/".
        json: Optional JSON payload to send.
        params: Optional query parameters.
        timeout: Optional request timeout; falls back to default.

    Returns:
        Parsed JSON data as dictionary.

    Raises:
        BackendError: When backend returns a non-200 status or invalid response.
    """
    base = BACKEND_BASE_URL.rstrip("/")
    url = base + path
    logger.info("Backend request: %s %s", method.upper(), url)

    t = httpx.Timeout(timeout or BACKEND_TIMEOUT, connect=BACKEND_CONNECT_TIMEOUT)

    try:
        async with httpx.AsyncClient(timeout=t) as client:
            resp = await client.request(method=method, url=url, json=json, params=params)
    except httpx.ConnectError as exc:
        logger.warning("Backend connect error: %s", exc)
        raise BackendError("Сервер временно недоступен. Попробуйте позже.") from exc
    except httpx.TimeoutException as exc:
        logger.warning("Backend timeout: %s", exc)
        raise BackendError("Сервер отвечает слишком долго. Попробуйте позже.") from exc
    except Exception as exc:
        logger.exception("Backend unexpected error: %s", exc)
        raise BackendError("Ошибка соединения с сервером. Попробуйте позже.") from exc

    try:
        payload = resp.json()
    except Exception:
        logger.warning("Backend returned non-JSON: %s", resp.text[:500])
        raise BackendError(f"Сервер вернул некорректный ответ (HTTP {resp.status_code}).")

    if resp.status_code >= 400:
        detail = _extract_backend_detail(payload, resp.status_code)
        logger.warning("Backend error %s: %s", resp.status_code, detail)
        raise BackendError(detail)

    if not isinstance(payload, dict):
        raise BackendError("Сервер вернул неожиданный формат данных.")
    return payload