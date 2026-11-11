"""CRC32C (Castagnoli), the RFC 3720 / iSCSI variant.

Reflected polynomial 0x82F63B78, init and final XOR 0xFFFFFFFF.
`crc32c(data, crc)` continues from a previous final value, so
`crc32c(b, crc32c(a)) == crc32c(a + b)`.

The pure-Python path always works (CPython, GraalPy, PyPy). GraalPy's JIT
compiles the loop. A C extension (google-crc32c, which pyproject.toml declares
for CPython, or crc32c) is used when it imports.
"""

_POLY = 0x82F63B78

import os  # noqa: E402


def _make_table():
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ _POLY if c & 1 else c >> 1
        table.append(c)
    return tuple(table)


_TABLE = _make_table()


def crc32c_pure(data, crc=0):
    if isinstance(data, memoryview) and data.format != "B":
        data = data.cast("B")
    table = _TABLE
    c = crc ^ 0xFFFFFFFF
    for b in data:
        c = table[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def _select():
    if os.environ.get("BREADCRUMBS_CRC32C") == "pure":  # force the pure loop (benchmarks)
        return crc32c_pure, "pure"
    try:
        import google_crc32c  # type: ignore

        if google_crc32c.implementation == "c":
            def crc32c_google(data, crc=0):
                return google_crc32c.extend(crc, bytes(data))

            return crc32c_google, "google-crc32c"
    except Exception:
        pass
    try:
        import crc32c as _ext  # type: ignore

        def crc32c_ext(data, crc=0):
            return _ext.crc32c(bytes(data), crc)

        return crc32c_ext, "crc32c"
    except Exception:
        pass
    return crc32c_pure, "pure"


crc32c, BACKEND = _select()
