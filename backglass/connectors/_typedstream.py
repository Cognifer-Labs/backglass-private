"""Recover the visible text from an `attributedBody` typedstream blob.

Vendored technique, not a dependency. The NSString-marker scan is the one from
`imessage_tools` (MIT, github.com/my-other-github-account/imessage_tools); the byte-level
format facts come from ReagentX/imessage-exporter's documentation of Apple's typedstream
(chrissardegna.com/blog/reverse-engineering-apples-typedstream-format/).

The format, only as far as we need it
-------------------------------------
An `attributedBody` is a serialized `NSMutableAttributedString`: a `streamtyped` header,
then the class chain, then the backing string, then the attribute runs. The backing string
is the first thing after the literal class name `NSString`, introduced by the typedstream
type code `+` (a length-prefixed byte array). Then comes the length:

  * a single byte < 0x80 is the length itself — the common case, short texts;
  * `\\x81` means the next 2 bytes are a little-endian u16 length;
  * `\\x82` means the next 4 bytes are a little-endian u32 length.

The bytes that follow are UTF-8. Everything after that — `NSDictionary`, the attribute
runs, `NSNumber` ranges — is styling we do not want and never read.

This is a scan, not a parser: it cannot see a second string and does not try. Any
malformed, truncated, or simply unfamiliar blob returns None rather than raising, because
a garbage blob must degrade to "this row had no text", never to a failed sync.
"""

from __future__ import annotations

import struct

_MARKER = b"NSString"
#: `+` follows the class name within a few bytes of version/reference codes.
_TYPE_CODE_WINDOW = 16


def extract_text(blob: bytes) -> str | None:
    """The visible text of an `attributedBody`, or None if it cannot be read."""
    try:
        start = blob.index(_MARKER) + len(_MARKER)
        plus = blob.find(b"+", start, start + _TYPE_CODE_WINDOW)
        if plus == -1:
            return None
        at = plus + 1
        length = blob[at]
        at += 1
        if length == 0x81:
            (length,) = struct.unpack_from("<H", blob, at)
            at += 2
        elif length == 0x82:
            (length,) = struct.unpack_from("<I", blob, at)
            at += 4
        elif length >= 0x80:
            return None  # some other escape; not a form we claim to understand
        text = blob[at : at + length]
        if len(text) != length:
            return None  # truncated blob
        return text.decode("utf-8").strip() or None
    except Exception:
        return None
