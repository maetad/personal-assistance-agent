"""qr-code: generates a printable-quality QR code PNG from text via the ``/qr`` slash command."""

from __future__ import annotations

import hashlib
import logging

import qrcode
from qrcode.constants import ERROR_CORRECT_M

logger = logging.getLogger("plugins.qr-code")

_HELP_TEXT = "Usage: /qr <text>\nGenerates a printable QR code PNG from your text."

# box_size + dpi are what make this legible printed, not just on a phone screen.
_BOX_SIZE = 20
_BORDER = 4
_DPI = (300, 300)


def _generate_qr_png(text: str) -> str:
    """Render ``text`` as a QR code PNG, return its absolute path. Raises ValueError if
    ``text`` is too long to fit any QR version."""
    from hermes_constants import get_hermes_home  # lazy: keeps module host-importable/testable

    cache_dir = get_hermes_home() / "cache" / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] + ".png"
    path = cache_dir / filename
    if path.exists():
        return str(path)

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=_BOX_SIZE, border=_BORDER)
    qr.add_data(text)
    qr.make(fit=True)  # raises ValueError when text exceeds max QR capacity
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(path, dpi=_DPI)
    return str(path)


def _handle_slash(raw_args: str) -> str:
    text = raw_args.strip()
    if not text:
        return _HELP_TEXT
    try:
        path = _generate_qr_png(text)
    except ValueError:
        return "That text is too long to encode as a QR code. Try something shorter."
    return f"MEDIA:{path}"


def register(ctx) -> None:
    ctx.register_command("qr", handler=_handle_slash,
                         description="Generate a printable QR code PNG from text.",
                         args_hint="<text>")
