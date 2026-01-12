"""Utility functions for QR code generation and file name sanitization."""

from __future__ import annotations

import io
import re
from typing import Optional

import qrcode

__all__ = [
    "build_qr_png_bytes",
    "safe_filename",
]


def build_qr_png_bytes(text: str) -> bytes:
    """
    Build a QR code PNG image from the given text and return it as bytes.

    Args:
        text: The content to encode into the QR code.

    Returns:
        PNG image bytes representing the QR code.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


def safe_filename(name: Optional[str], default: str = "wireguard.conf") -> str:
    """
    Sanitize a string to be used as a filename. Non-alphanumeric characters
    (except spaces, underscores, hyphens, dots and parentheses) are replaced
    with underscores. Ensures the filename ends with .conf.

    Args:
        name: Original name (may be None or empty).
        default: Fallback filename if name is invalid.

    Returns:
        Safe filename string.
    """
    n = (name or "").strip()
    if not n:
        return default
    n = re.sub(r"[^0-9a-zA-Zа-яА-Я _\-\.\(\)]", "_", n)
    n = n.strip()
    if not n:
        return default
    if not n.lower().endswith(".conf"):
        n += ".conf"
    return n