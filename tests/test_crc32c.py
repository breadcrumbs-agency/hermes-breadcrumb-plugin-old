import unittest

from breadcrumbs.crc32c import crc32c, crc32c_pure


class Crc32cVectors(unittest.TestCase):
    """RFC 3720 (iSCSI) CRC32C test vectors, plus the common check value."""

    def test_check_value_123456789(self):
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)

    def test_empty_is_zero(self):
        self.assertEqual(crc32c(b""), 0)

    def test_rfc3720_32_zero_bytes(self):
        self.assertEqual(crc32c(bytes(32)), 0x8A9136AA)

    def test_rfc3720_32_ff_bytes(self):
        self.assertEqual(crc32c(b"\xff" * 32), 0x62A8AB43)

    def test_rfc3720_ascending(self):
        self.assertEqual(crc32c(bytes(range(32))), 0x46DD794E)

    def test_rfc3720_descending(self):
        self.assertEqual(crc32c(bytes(range(31, -1, -1))), 0x113FDB5C)

    def test_incremental_equals_one_shot(self):
        self.assertEqual(crc32c(b"6789", crc32c(b"12345")), 0xE3069283)

    def test_accepts_memoryview(self):
        self.assertEqual(crc32c(memoryview(b"xx123456789")[2:]), 0xE3069283)

    def test_pure_matches_selected_backend_on_long_input(self):
        data = bytes((i * 131 + 7) & 0xFF for i in range(10_001))
        self.assertEqual(crc32c_pure(data), crc32c(data))


if __name__ == "__main__":
    unittest.main()
