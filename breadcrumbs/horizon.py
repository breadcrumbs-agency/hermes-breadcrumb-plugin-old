"""Horizon: a steady-state, adaptive, prioritized recall budget.

decay()      weight of a crumb by age and idle time (two half-lives) and a floor.
split()      split a token budget across crumbs by weight (largest remainder).
Controller   holds the recall budget where the context overflows on 2%..5% of turns:
             an overflow while the rate is above the band cuts the budget (x0.9);
             a clean turn while the rate is below the band grows it (x1.01);
             inside the band nothing changes. The rate is taken over the last 50 turns.
"""

from collections import deque


def decay(now, created, accessed, age_half, idle_half, floor=0.01):
    """0.5 ** (age / age_half + idle / idle_half); a value under `floor` becomes 0."""
    if accessed is None:
        accessed = created
    age = max(0.0, now - created) / age_half
    idle = max(0.0, now - accessed) / idle_half
    w = 0.5 ** (age + idle)
    return w if w >= floor else 0.0


def split(total, weights):
    """Integer shares of `total` in proportion to `weights`; the shares sum to `total`."""
    if not weights:
        return []
    w = [max(0.0, float(x)) for x in weights]
    s = sum(w)
    if s <= 0:
        w, s = [1.0] * len(w), float(len(w))
    exact = [total * x / s for x in w]
    shares = [int(e) for e in exact]
    left = total - sum(shares)
    for i in sorted(range(len(w)), key=lambda i: (shares[i] - exact[i], i))[:left]:
        shares[i] += 1
    return shares


class Controller:
    def __init__(self, budget=300, lo=40, hi=2000, window=50, band=(0.02, 0.05), down=0.9, up=1.01):
        self.budget, self.lo, self.hi = int(budget), int(lo), int(hi)
        self.window, self.band, self.down, self.up = int(window), tuple(band), float(down), float(up)
        self.ring = deque(maxlen=self.window)

    @property
    def rate(self):
        return sum(self.ring) / len(self.ring) if self.ring else 0.0

    def observe(self, overflow):
        """Record one turn. Returns "back", "up" or "hold"."""
        self.ring.append(1 if overflow else 0)
        rate = self.rate
        if overflow and rate > self.band[1]:
            self.budget = max(self.lo, int(self.budget * self.down))
            return "back"
        if not overflow and rate < self.band[0]:
            self.budget = min(self.hi, max(self.budget + 1, int(self.budget * self.up)))
            return "up"
        return "hold"

    def to_dict(self):
        return {"budget": self.budget, "lo": self.lo, "hi": self.hi, "window": self.window,
                "band": list(self.band), "down": self.down, "up": self.up,
                "ring": "".join(str(x) for x in self.ring)}

    @classmethod
    def from_dict(cls, d):
        c = cls(d["budget"], d["lo"], d["hi"], d["window"], d["band"], d["down"], d["up"])
        c.ring.extend(int(ch) for ch in d.get("ring", ""))
        return c
