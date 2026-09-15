"""Build a small PDF with a real page layout, for extraction tests.

The tests need a two-column page whose columns a layout-preserving extractor
would interleave. Writing the PDF here keeps the fixture readable and keeps a
binary out of the repository.
"""

from __future__ import annotations


_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842
_LEADING = 14


def two_column_pdf(
    left: list[str], right: list[str], *, heading: str | None = None
) -> bytes:
    """Return a one-page PDF with the two given columns side by side."""
    commands = []
    top = _PAGE_HEIGHT - 60
    if heading:
        commands.append(_text(60, top, heading, size=16))
        top -= 40
    for index, line in enumerate(left):
        commands.append(_text(60, top - index * _LEADING, line))
    for index, line in enumerate(right):
        commands.append(_text(320, top - index * _LEADING, line))
    return _document("\n".join(commands))


def _text(x: int, y: int, value: str, *, size: int = 10) -> str:
    escaped = value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET"


def _document(content: str) -> bytes:
    stream = content.encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}]"
            " /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n"
    ).encode("ascii")
    out += b"%%EOF\n"
    return bytes(out)
