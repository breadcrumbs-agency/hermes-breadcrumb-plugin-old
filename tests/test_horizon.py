import random
import unittest

from breadcrumbs.horizon import Controller, decay, split


class SplitTest(unittest.TestCase):
    def test_largest_remainder_sums_exactly(self):
        self.assertEqual(split(10, [1, 1, 1]), [4, 3, 3])
        self.assertEqual(split(100, [3, 1]), [75, 25])
        self.assertEqual(sum(split(997, [0.3, 2.1, 5.7, 0.01])), 997)

    def test_all_zero_weights_split_equally(self):
        self.assertEqual(split(7, [0, 0]), [4, 3])

    def test_negative_weights_count_as_zero(self):
        self.assertEqual(split(9, [-5, 1, 2]), [0, 3, 6])

    def test_empty(self):
        self.assertEqual(split(5, []), [])


class DecayTest(unittest.TestCase):
    def test_half_lives(self):
        self.assertAlmostEqual(decay(0, 0, 0, 10, 5), 1.0)
        self.assertAlmostEqual(decay(10, 0, 10, 10, 5), 0.5)   # one age half-life
        self.assertAlmostEqual(decay(5, 5 - 10 * 0, 0, 10, 5), 0.5)  # one idle half-life, age 0
        self.assertAlmostEqual(decay(10, 0, 5, 10, 5), 0.25)   # one of each

    def test_never_accessed_uses_created(self):
        self.assertAlmostEqual(decay(10, 0, None, 10, 10), 0.25)

    def test_below_floor_is_zero(self):
        self.assertEqual(decay(1000, 0, 0, 10, 10, floor=0.01), 0.0)


class ControllerTest(unittest.TestCase):
    def test_overflow_backs_off_to_the_floor(self):
        c = Controller(budget=1000, lo=50, hi=2000)
        for _ in range(200):
            c.observe(True)
        self.assertEqual(c.budget, 50)

    def test_no_overflow_grows_to_the_ceiling(self):
        c = Controller(budget=100, lo=50, hi=2000)
        for _ in range(400):  # x1.01 per clean turn: 356 turns from 100 to 2000
            c.observe(False)
        self.assertEqual(c.budget, 2000)

    def test_rate_in_band_holds(self):
        c = Controller(budget=500, window=50)
        for i in range(50):
            c.observe(i in (10, 30))  # 2 of 50 = 4%, inside 2%..5%
        b = c.budget
        self.assertEqual(c.observe(False), "hold")  # 2 of 50 still
        self.assertEqual(c.budget, b)

    def test_converges_to_a_steady_state(self):
        # Overflow when budget plus noise passes a hidden limit: a context that the
        # controller cannot see. The rate must settle near the 2%..5% band.
        rng = random.Random(7)
        c = Controller(budget=200, lo=40, hi=4000)
        hist = []
        for _ in range(3000):
            over = c.budget + rng.gauss(0, 100) > 900
            c.observe(over)
            hist.append((c.budget, over))
        tail = hist[-1000:]
        rate = sum(o for _, o in tail) / len(tail)
        budgets = [b for b, _ in tail]
        self.assertGreater(rate, 0.015)  # the band is 2%..5%
        self.assertLess(rate, 0.06)
        self.assertGreater(min(budgets), 500)
        self.assertLess(max(budgets), 900)

    def test_state_roundtrip(self):
        c = Controller(budget=321)
        for o in (True, False, False):
            c.observe(o)
        d = Controller.from_dict(c.to_dict())
        self.assertEqual((d.budget, list(d.ring)), (c.budget, list(c.ring)))


if __name__ == "__main__":
    unittest.main()
