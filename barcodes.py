"""Inline-SVG barcode helpers for the printable ticket sheet.

Both generators return an SVG string suitable for direct embedding in a
template (render with ``|safe``). Pure-Python, no system dependencies.
"""

import io
import re

import segno
from barcode import Code128
from barcode.writer import SVGWriter

_XML_PROLOG = re.compile(rb"<\?xml[^>]*\?>\s*(<!DOCTYPE[^>]*>\s*)?")


def code128_svg(text):
    """Code 128 of ``text`` (e.g. the bare ticket ID) as an inline SVG string."""
    buf = io.BytesIO()
    Code128(str(text), writer=SVGWriter()).write(
        buf,
        options={
            "module_width": 0.33,  # mm per narrow bar; wide enough for mid-range scanners
            "module_height": 13.0,
            "quiet_zone": 4.0,
            "font_size": 8,
            "text_distance": 3.5,
        },
    )
    return _XML_PROLOG.sub(b"", buf.getvalue()).decode("utf-8")


def qr_svg(data, scale=3):
    """QR code of ``data`` (e.g. the portal URL) as an inline SVG string."""
    buf = io.BytesIO()
    segno.make(str(data), error="m").save(buf, kind="svg", scale=scale, border=2)
    return _XML_PROLOG.sub(b"", buf.getvalue()).decode("utf-8")
