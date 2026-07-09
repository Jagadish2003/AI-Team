"""
R18-A1 / T2 — helpers shared by the OOXML handlers (docx, xlsx, pptx).

The modern Office formats (.docx/.xlsx/.pptx) are all ZIP-packaged OOXML. When a
file is password-protected, Office wraps that ZIP in an OLE Compound File — so an
encrypted Office document starts with the OLE magic bytes, NOT the ZIP magic. That
one check lets every OOXML handler tell "encrypted → loud skip" (AC3) apart from
"corrupt → raise" without depending on each parser's idiosyncratic error message.
"""
from __future__ import annotations

#: OLE Compound File header — the container Office uses for an ENCRYPTED document.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: ZIP local-file header — the container of a normal (unencrypted) OOXML document.
_ZIP_MAGIC = b"PK\x03\x04"


def looks_encrypted_ooxml(raw: bytes) -> bool:
    """True when the bytes are an OLE compound file — i.e. an encrypted OOXML doc.

    A password-protected .docx/.xlsx/.pptx is delivered as an OLE container rather
    than a ZIP, so this is a reliable, parser-independent encryption signal (AC3).
    """
    return bool(raw) and raw[:8] == _OLE_MAGIC


def is_ooxml_zip(raw: bytes) -> bool:
    """True when the bytes begin with the ZIP magic (a normal OOXML package)."""
    return bool(raw) and raw[:4] == _ZIP_MAGIC
