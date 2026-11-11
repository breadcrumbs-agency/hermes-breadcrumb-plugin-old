"""Crumb encoding: one byte string for one crumb, on every runtime and every encoder path."""
import json
import unittest

from breadcrumbs import crumbs
from breadcrumbs.crumbs import Crumb

META = {"ev": "open", "ovf": False, "cap": True, "lv": [0.5, 1.0], "q": "tea \u00e9t\u00e9 \u2013 kettle", "n": None,
        "band": [0.02, 0.05], "items": ["a1b2c3d4", "e5f6a7b8:0-3"], "nested": {"z": 1, "a": [False, True]}}


class EncodeTest(unittest.TestCase):
    def test_the_pure_python_encoder_gives_the_same_bytes(self):
        """GraalPy uses the pure-Python encoder (its native encoder can reject False); the bytes, and so
        the crumb ids, must not change."""
        c = Crumb("rcpt", "open", ranges=[["e5f6a7b8", 0, 3]], kids=["a1b2c3d4"], meta=META)
        one_shot = json.dumps({"k": "rcpt", "t": "open", "r": [["e5f6a7b8", 0, 3]], "c": ["a1b2c3d4"], "m": META},
                              sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(crumbs._dumps({"k": "rcpt", "t": "open", "r": [["e5f6a7b8", 0, 3]], "c": ["a1b2c3d4"],
                                        "m": META}, pure=True), one_shot)
        self.assertEqual(c.encode(), crumbs.MAGIC + one_shot.encode("utf-8"))
        self.assertEqual(Crumb.decode(c.encode()).meta, META)


if __name__ == "__main__":
    unittest.main()
