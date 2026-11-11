#!/usr/bin/env python3
"""Score for the v12 film: a kung fu showdown, played on the film's own clock.

  python3.14 score.py film-meta.json score.wav   (NumPy is in python3.14, not in the system python3)

The brief (Nov 10): "the kung fu showdown background track, akin to a country western showdown with wind and
woodwind, and ambient tension gearing up", "and wooden block hits", and the woodwind is a dizi. The sounds that came
with the fork of Ascension's score (the Shepard rise, the noise sweeps, the chord pads, the marimba, the bells, the
ticks and the hats) were a mistake: none of them is left. Every sound is made by the code below; no sample is used.

The bed comes from the film's clock and its ascent curve. It does not depend on the events.
  wind     gusts over an open plain, in stereo, with whistles in it. A gust grows while the camera flies, and the wind
           grows from level to level.
  tension  a low bowed drone on B that gears up. It comes in after the first level; each level makes it louder and
           brighter and makes its tremolo faster, and more strings join it. Over the last seconds it falls away, so the
           end is the wind of the start (the film loops).
  call     the dizi alone on the wind: a phrase at the start, and one at each of the two landings nearest to one third
           and two thirds of the film. A canyon gives it back.
The events. A level asks for a sound with an event row [u, kind, pitch, vel, pan]. The pitch is a step of the
pentatonic D E F# A B (midi() below), and a rising run of steps is a rising run of pitches.
  wood          a wood block: a dry, hollow 'tok' (each address that opens)
  drum          a tang drum: skin, stick and a pitch that settles (each fold)
  flute         the dizi: a bamboo flute with the buzz of its membrane, a slide up into the note and a vibrato that
                grows (the long climbs). Flute events in a row are one line: while the next flute event is within 2.4 s,
                a note fades out over 0.1 s as that one starts. A lone note, and the last of a line, rings 1.8 s.
  pluck, bell, chord   temple blocks: pitched hollow wood (a chord is three of them, rolled)
  tick, hat     the clapper (paiban): a small, dry crack of wood
  thump, kick   the big drum (dagu) with a soft beater: the landing of each flight
  gliss         the dizi slides up an octave
Contracts for the tests (test_score.py): the loudest sample of the dry sound is 0.9*vel for wood, 1.0*vel for drum,
0.5*vel for flute. Wood sounds at the pitch of midi() (D4 is step 0 on level 0), the drum two octaves under it and the
flute one octave over it. Each event has its own noise source, so an event changes no other sound and not the bed.
Each kind enters the mix at HIT times its dry sound, and the reverb at SEND times that.

The master sets the file to -18 LUFS integrated (BS.1770, gated), with no sample above -3.1 dBFS (see lufs() below).
"""
import json
import sys
import wave

import numpy as np

SR = 48000
meta = json.load(open(sys.argv[1]))
DUR = float(meta['duration'])
N = int(round(DUR * SR))
t_all = np.arange(N) / SR
F_ALL = np.fft.rfftfreq(N, 1 / SR)

levels = meta.get('levels') or [{'zs': -1.0, 'ze': 0.0, 'de': DUR}]
last = len(levels) - 1
if meta.get('curve') and len(meta['curve']) > 2:
    curve = np.array(meta['curve'], float)
    tc = np.arange(len(curve)) / 100.0
else:                                                    # a meta with no curve: the levels climb evenly
    curve, tc = np.array([0.0, float(last)]), np.array([0.0, DUR])
L = np.interp(t_all, tc, curve)
speed = np.convolve(np.abs(np.gradient(curve)) * 100.0, np.ones(21) / 21, mode='same')
v = np.interp(t_all, tc, np.clip(speed / max(speed.max(), 1e-9), 0, 1))     # camera speed, 0 to 1


def ease(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


CLOSE = min(3.5, 0.25 * DUR)                            # the tension falls away over the last CLOSE seconds
close = ease((DUR - t_all) / CLOSE)
gear = (np.clip(L / last, 0, 1) if last else np.clip(t_all / DUR, 0, 1)) * close

mix = np.zeros((2, N))
send = np.zeros((2, N))


def add(buf, start, sig, pan=0.0):
    """Add a mono signal at sample `start`, equal-power panned; whatever runs past the end wraps."""
    a = (pan + 1) * np.pi / 4
    gains = (np.cos(a), np.sin(a))
    idx = (start + np.arange(len(sig))) % N
    for ch in (0, 1):
        np.add.at(buf[ch], idx, sig * gains[ch])


PENT = [0, 2, 4, 7, 9]
hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)


def midi(p, level):
    q = p + level % 5
    m = 62 + 12 * (q // 5) + PENT[q % 5]
    while m > 93:
        m -= 12
    while m < 38:
        m += 12
    return m


def reg(m, lo, hi):
    """The note m moved by octaves into [lo, hi)."""
    while m >= hi:
        m -= 12
    while m < lo:
        m += 12
    return m


def band_noise(r, n, fc, width):
    """n samples of noise with unit rms, in a band of `width` octaves round fc Hz."""
    F = np.fft.rfftfreq(n, 1 / SR)
    x = np.fft.irfft(np.fft.rfft(r.standard_normal(n)) * np.exp(-0.5 * (np.log2((F + 1) / fc) / width) ** 2), n)
    return x / (x.std() + 1e-12)


def tail_off(s, ms):
    k = int(ms / 1000 * SR)
    s[-k:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, k))
    return s


def own_rng(e, salt):
    """The noise source of one event, so that an event moves no other sound."""
    return np.random.default_rng([7, salt, int(round(abs(e['t']) * SR)), abs(int(e['p'])) % 997])


def level_to(s, e, peak):
    """s scaled so that its loudest sample is peak * vel: the loudness does not depend on the pitch."""
    return s * (peak * e['vel'] / max(np.abs(s).max(), 1e-12))


# ── the dizi ────────────────────────────────────────────────────
def dizi(f0, dur, r, peak, rel=0.12, scoop=-1.2, fall=0.0, vib=30.0, path=None):
    """A dizi note of `dur` s at f0 Hz whose loudest sample is `peak`. A tongued start (a puff of air, and a slide up
    into the pitch from `scoop` semitones), a vibrato that comes in after a beat and widens, harmonics that brighten as
    the breath grows, the buzz of the membrane (dimo) once in each cycle, breath noise tuned to the harmonics, a fall of
    `fall` semitones in the last 0.3 s, and a breath that ends over `rel` s. path = (times, semitones): a slide."""
    n = int(round(dur * SR))
    t = np.arange(n) / SR
    rel, att = min(rel, 0.5 * dur), min(0.05, 0.3 * dur)
    semi = scoop * np.exp(-t / 0.05) if path is None else np.interp(t, *path)
    if fall:
        semi = semi + fall * np.clip((t - dur + 0.3) / 0.28, 0, 1) ** 2
    vib_on = np.clip((t - 0.2) / 0.6, 0, 1)
    vph = 2 * np.pi * np.cumsum(5.6 + 0.4 * np.sin(2 * np.pi * 0.45 * t + r.uniform(0, 2 * np.pi))) / SR
    knots = r.standard_normal(int(dur * 12) + 2)
    semi = semi + (vib / 100) * vib_on * np.sin(vph) + 0.03 * np.interp(t, np.linspace(0, dur, len(knots)), knots)
    ph = 2 * np.pi * np.cumsum(f0 * 2 ** (semi / 12)) / SR
    breath = ease(t / att) * (1 + 0.25 * ease((t - 0.3) / max(dur - 0.6, 0.3)))     # a long note swells
    breath = breath * (1 + 0.07 * vib_on * np.sin(vph - 0.6))                         # and pulses with the vibrato
    gate = 0.5 + 0.5 * np.cos(np.pi * np.clip((t - dur + rel) / rel, 0, 1))
    tone = np.zeros(n)
    for k in range(1, max(1, min(14, int(9000 / f0))) + 1):
        a = k ** -1.15 * (1 + 1.4 * np.exp(-0.5 * (np.log2(k * f0 / 3400) / 0.45) ** 2))   # the membrane's formant
        tone += a * np.sin(k * ph + 0.3 * k) * breath ** (1 + 0.35 * (k - 1))
    tone = np.tanh(1.6 * tone / (np.abs(tone).max() + 1e-12))                            # reedy
    buzz = 0.22 * band_noise(r, n, 4200, 0.7) * (0.5 + 0.5 * np.cos(ph)) ** 6 * breath ** 2
    F = np.fft.rfftfreq(n, 1 / SR)
    comb = sum(np.exp(-0.5 * ((F - k * f0) / (0.03 * k * f0 + 10)) ** 2) / k for k in range(1, 7))
    air = np.fft.irfft(np.fft.rfft(r.standard_normal(n)) * comb, n)
    air *= (0.08 * breath + 0.5 * np.exp(-t / 0.03) * ease(t / 0.004)) / (air.std() + 1e-12)
    s = (tone + buzz + air) * gate * (1 - np.exp(-t / 0.002))
    return s * (peak / (np.abs(s).max() + 1e-12))


# ── the bed: wind ───────────────────────────────────────────────
bed_rng = np.random.default_rng(11)


def drift(r, lo, hi, n=5):
    """A smooth random curve over the film in 0..1 that loops: cosines whose periods (lo to hi s) divide the film."""
    m = r.integers(max(1, int(round(DUR / hi))), max(1, int(round(DUR / lo))) + 1, n)
    x = sum(r.uniform(0.5, 1.0) * np.cos(2 * np.pi * mi * t_all / DUR + r.uniform(0, 2 * np.pi)) for mi in m)
    return (x - x.min()) / (np.ptp(x) + 1e-12)


def noise_band(r, H):
    """Noise over the whole film through the gain curve H (one FFT, so it loops), at unit rms."""
    x = np.fft.irfft(np.fft.rfft(r.standard_normal(N)) * H, N)
    return x / (x.std() + 1e-12)


lognorm = lambda fc, w: np.exp(-0.5 * (np.log2((F_ALL + 1) / fc) / w) ** 2)
HP = (F_ALL / 35) ** 2 / (1 + (F_ALL / 35) ** 2)
WIND, WHISTLES = 0.034, (540.0, 735.0, 980.0, 1310.0)
gust = (0.3 + 0.7 * drift(bed_rng, 4.0, 14.0) ** 1.6) * (0.85 + 0.3 * drift(bed_rng, 1.0, 3.5))
G = gust * (0.6 + 0.4 * gear) + 0.5 * v ** 2           # the camera's flight is a gust
rumble_c = noise_band(bed_rng, HP / (1 + (F_ALL / 120) ** 2))
wind = np.zeros((2, N))
for ch in (0, 1):
    w = (0.6 * rumble_c + 0.8 * noise_band(bed_rng, HP / (1 + (F_ALL / 120) ** 2))) * G
    w += 0.55 * noise_band(bed_rng, lognorm(430, 0.9)) * G ** 1.3
    w += 0.12 * noise_band(bed_rng, lognorm(2800, 1.0)) * G ** 2
    for fw in WHISTLES:
        whistle = drift(bed_rng, 1.5, 6.0) ** 4 * (0.25 + 0.75 * np.minimum(G, 1.5) / 1.5)
        w += 0.22 * noise_band(bed_rng, lognorm(fw * (1.03 if ch else 0.97), 0.02)) * whistle
    wind[ch] = WIND * w

# ── the bed: tension that gears up ──────────────────────────────
TENS = 0.06
TENS_VOICES = [(61.74, 1.0, 0.0, 0.0, -0.15), (92.50, 0.6, 0.25, 0.45, 0.2), (123.47, 0.45, 0.5, 0.7, -0.25),
               (146.83, 0.3, 0.75, 0.9, 0.3)]   # B1, F#2, B2, D3: Hz, level, gear where it starts and is full, pan
trem = 1 - (0.12 + 0.5 * gear) * (0.5 - 0.5 * np.cos(2 * np.pi * np.cumsum(0.7 * 2 ** (3.0 * gear)) / SR))
t_amp = TENS * ease(t_all / 5.0) * (0.2 + 0.8 * gear ** 1.1) * close * trem
tension = np.zeros((2, N))
for f0, a, g0, g1, pan in TENS_VOICES:
    dark, bright = np.zeros(N), np.zeros(N)
    for d in (0.9985, 1.0015):                          # two bows a hair apart: they beat
        ph = 2 * np.pi * np.cumsum(f0 * d * (1 + 0.002 * (drift(bed_rng, 0.5, 2.0, 3) - 0.5))) / SR
        for k in range(1, int(2400 / f0) + 1):
            s = np.sin(k * ph) / k
            dark += s / (1 + (k * f0 / 260.0) ** 4)
            bright += s / (1 + (k * f0 / 1400.0) ** 4)
    on = ease((gear - g0) / (g1 - g0)) if g1 > g0 else 1.0
    voice = a * on * ((1 - gear) * dark + gear * bright) * t_amp
    ang = (pan + 1) * np.pi / 4
    tension[0] += np.cos(ang) * voice
    tension[1] += np.sin(ang) * voice

# ── the bed: the call of the dizi ───────────────────────────────
# (step, start s, length s, slide in, fall at the end): B D B F# E on level 0, a lone call with a grace note in it
CALL = [(4, 0.00, 1.55, -2.0, 0.0), (5, 1.55, 0.22, -0.5, 0.0), (4, 1.77, 0.45, 0.0, 0.0), (2, 2.22, 0.40, 0.0, 0.0),
        (1, 2.62, 1.90, -0.8, -1.6)]
CALL_PEAK = 0.14
calls = [(0.35, 0)]
landings = [(lv['ze'], k) for k, lv in enumerate(levels) if k >= 1 and 4.0 < lv['ze'] < DUR - 6.0]
for frac in (1 / 3, 2 / 3):
    if landings:
        ze, k = min(landings, key=lambda x: abs(x[0] - frac * DUR))
        if all(abs(ze - c) > 6.0 for c, _ in calls):
            calls.append((ze, k))
call = np.zeros(N)
for c0, lv in calls:
    notes = [midi(p, lv) + 12 for p, *_ in CALL]
    shift = 0
    while max(notes) + shift > 93:
        shift -= 12
    for (p, s, ln, sc, fl), m in zip(CALL, notes):
        sig = dizi(hz(m + shift), ln + 0.08, bed_rng, CALL_PEAK, rel=0.08 if not fl else 0.35, scoop=sc, fall=fl)
        idx = (int(round((c0 + s) * SR)) + np.arange(len(sig))) % N
        np.add.at(call, idx, sig)
add(send, 0, call, 0.1)

bed = wind + tension
bed[0] += call * np.cos(1.1 * np.pi / 4)
bed[1] += call * np.sin(1.1 * np.pi / 4)
mix += bed


# ── the events ──────────────────────────────────────────────────
FLUTE_NEXT, FLUTE_LAST = 2.4, 1.8   # a flute note joins the next flute event if that is within FLUTE_NEXT s; a lone note rings FLUTE_LAST s
FLUTE_LAP = 0.10                    # a joined note fades out over this long, from the moment the next one starts


def wood(e):
    """A wood block: a hollow 'tok'. Three modes that die fast (the high ones first), the knock of the stick, and a
    pitch that settles in the first few ms."""
    f = hz(reg(midi(int(e['p']), e['level']), 60, 96))
    r = own_rng(e, 1)
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    ph = 2 * np.pi * f * np.cumsum(1 + 0.02 * np.exp(-t / 0.004)) / SR
    body = (np.sin(ph) * np.exp(-t / 0.038) + 0.32 * np.sin(2.57 * ph + 0.4) * np.exp(-t / 0.016)
            + 0.12 * np.sin(4.13 * ph + 1.1) * np.exp(-t / 0.008))
    knock = 0.3 * band_noise(r, n, 900, 1.3) * np.exp(-t / 0.0018)
    return tail_off(level_to((body + knock) * (1 - np.exp(-t / 0.0004)), e, 0.9), 20)


def drum(e):
    """A tang drum: a skin whose pitch starts high and settles, three modes of the skin above it, the stick on the
    skin, and the wooden shell."""
    f = hz(reg(midi(int(e['p']), e['level']) - 24, 36, 72))
    r = own_rng(e, 2)
    n = int(1.2 * SR)
    t = np.arange(n) / SR
    ph = 2 * np.pi * f * np.cumsum(1 + 0.4 * np.exp(-t / 0.03)) / SR
    body = (np.sin(ph) * np.exp(-t / 0.13) + 0.4 * np.sin(1.593 * ph) * np.exp(-t / 0.06)
            + 0.25 * np.sin(2.135 * ph) * np.exp(-t / 0.045) + 0.12 * np.sin(2.653 * ph) * np.exp(-t / 0.03))
    stick = 0.5 * band_noise(r, n, 1500, 0.9) * np.exp(-t / 0.005)
    shell = 0.15 * np.sin(2 * np.pi * 410 * t) * np.exp(-t / 0.02)
    return tail_off(level_to((body + stick + shell) * (1 - np.exp(-t / 0.0005)), e, 1.0), 50)


def flute(e):
    """The dizi on a flute event. e['hold'] = (seconds it rings, seconds of its fade-out) is set by the loop over the
    flute line below; an event without it is a lone note (or the last of a line): it rings FLUTE_LAST s and falls."""
    f0 = hz(reg(midi(int(e['p']), e['level']) + 12, 60, 106))
    joined = 'hold' in e
    dur, rel = e['hold'] if joined else (FLUTE_LAST, 0.4)
    return dizi(f0, dur, own_rng(e, 3), 0.5 * e['vel'], rel=rel, scoop=-0.5 if e.get('legato') else -1.2,
                fall=0.0 if joined else -0.8)


def temple(e, decay, salt):
    """A temple block: pitched hollow wood, a round 'bok'."""
    f = hz(reg(midi(int(e['p']), e['level']), 60, 96))
    r = own_rng(e, salt)
    n = int((7 * decay + 0.05) * SR)
    t = np.arange(n) / SR
    ph = 2 * np.pi * f * np.cumsum(1 + 0.012 * np.exp(-t / 0.005)) / SR
    body = (np.sin(ph) * np.exp(-t / decay) + 0.2 * np.sin(2.11 * ph + 0.5) * np.exp(-t / (0.3 * decay))
            + 0.07 * np.sin(3.71 * ph + 1.3) * np.exp(-t / (0.15 * decay)))
    knock = 0.22 * band_noise(r, n, 1100, 1.2) * np.exp(-t / 0.0018)
    return tail_off((body + knock) * (1 - np.exp(-t / 0.0004)), 20)


def clapper(e):
    """The clapper (paiban): two slats of hard wood, a small dry crack."""
    r = own_rng(e, 5)
    n = int(0.06 * SR)
    t = np.arange(n) / SR
    f = 1500.0 * r.uniform(0.96, 1.04)
    body = sum(a * np.sin(2 * np.pi * f * m * t + r.uniform(0, 2 * np.pi)) * np.exp(-t / d)
               for m, a, d in ((1.0, 1.0, 0.006), (1.71, 0.5, 0.0035), (2.63, 0.25, 0.002)))
    crack = 0.35 * band_noise(r, n, 2600, 0.9) * np.exp(-t / 0.0009)
    return tail_off((body + crack) * (1 - np.exp(-t / 0.0002)), 8)


def dagu(e, f0):
    """The big drum (dagu) with a soft beater: a deep skin that settles, and the thud of the beater."""
    r = own_rng(e, 6)
    n = int(1.3 * SR)
    t = np.arange(n) / SR
    ph = 2 * np.pi * np.cumsum(f0 * (1 + 0.55 * np.exp(-t / 0.045))) / SR
    body = (np.sin(ph) * np.exp(-t / 0.36) + 0.3 * np.sin(1.52 * ph) * np.exp(-t / 0.14)
            + 0.12 * np.sin(2.18 * ph) * np.exp(-t / 0.07))
    beater = 0.3 * band_noise(r, n, 420, 1.2) * np.exp(-t / 0.012)
    return tail_off((body + beater) * (1 - np.exp(-t / 0.0015)), 60)


def synth(e):
    kind = e['kind']
    if kind == 'wood':
        return wood(e)
    if kind == 'drum':
        return drum(e)
    if kind == 'flute':
        return flute(e)
    if kind == 'pluck':
        return level_to(temple(e, 0.075, 4), e, 0.8)
    if kind == 'bell':
        return level_to(temple(e, 0.13, 7), e, 0.9)
    if kind == 'chord':
        out = np.zeros(int(1.0 * SR))
        for i, dp in enumerate((0, 2, 4)):
            s = level_to(temple({**e, 'p': e['p'] + dp}, 0.1, 8 + i), e, 0.6)
            k = int(i * 0.03 * SR)
            out[k:k + len(s)] += s[:len(out) - k]
        return out
    if kind == 'tick':
        return level_to(clapper(e), e, 0.6)
    if kind == 'hat':
        return level_to(clapper(e), e, 0.5)
    if kind == 'thump':
        return level_to(dagu(e, 50.0), e, 1.0)
    if kind == 'kick':
        return level_to(dagu(e, 58.0), e, 1.0)
    if kind == 'gliss':
        f0 = hz(reg(midi(int(e['p']), e['level']) + 12, 60, 96))
        return dizi(f0, 1.2, own_rng(e, 9), 0.5 * e['vel'], rel=0.3, path=(np.array([0.0, 0.9, 1.2]), np.array([0.0, 12.0, 12.0])))
    raise ValueError(kind)


# the flute is one line: while the next flute event is near, a note fades out as that one starts, so the notes join
line = sorted((e for e in meta['events'] if e['kind'] == 'flute'), key=lambda e: e['t'])
for e, nxt in zip(line, line[1:] + [None]):
    gap = nxt['t'] - e['t'] if nxt else FLUTE_NEXT + 1
    if gap <= FLUTE_NEXT:
        e['hold'] = (max(gap + FLUTE_LAP, 0.15), FLUTE_LAP)
        nxt['legato'] = True

# each kind enters the mix at HIT times its dry sound and the reverb at SEND times that: the hits sit in the wind
HIT = {'wood': 0.4, 'drum': 0.5, 'flute': 0.8, 'pluck': 0.45, 'bell': 0.45, 'chord': 0.45, 'tick': 0.35, 'hat': 0.35,
       'thump': 0.55, 'kick': 0.5, 'gliss': 0.7}
SEND = {'wood': 0.4, 'drum': 0.3, 'flute': 1.0, 'pluck': 0.5, 'bell': 0.6, 'chord': 0.5, 'tick': 0.15, 'hat': 0.15,
        'thump': 0.25, 'kick': 0.25, 'gliss': 1.0}
hits = np.zeros((2, N))
for e in meta['events']:
    sig = synth(e) * HIT.get(e['kind'], 1.0)
    s0 = int(round(e['t'] * SR))
    add(hits, s0, sig, e.get('pan', 0))
    add(send, s0, sig * SEND.get(e['kind'], 0.5), e.get('pan', 0))
mix += hits

# ── open air and a canyon on the send: a dark, short reverb and three echoes. It is circular: the end rings into the start ──
rv = np.random.default_rng(5)
t = np.arange(int(2.4 * SR)) / SR
ir = rv.standard_normal(len(t)) * np.exp(-t / 0.42)
ir = np.convolve(ir, np.ones(8) / 8, mode='same')
ir[:int(0.015 * SR)] = 0
ir *= 0.30 / np.sqrt(np.sum(ir ** 2))
blip = np.hanning(29)
blip /= blip.sum()
for d, g in ((0.43, 0.22), (0.86, 0.09), (1.29, 0.035)):
    k = int(d * SR)
    ir[k:k + len(blip)] += g * blip
pad_ir = np.zeros(N)
pad_ir[:min(len(ir), N)] = ir[:N]
IR_F = np.fft.rfft(pad_ir)
for ch in (0, 1):
    mix[ch] += np.fft.irfft(np.fft.rfft(send[ch]) * IR_F, n=N)


def lufs(x):
    """Integrated loudness of a stereo signal in LUFS (ITU-R BS.1770-4): the K-weighting filter (its two 48 kHz biquads,
    applied in the frequency domain, which suits a score that loops), mean square in 400 ms blocks with 75% overlap, an
    absolute gate at -70 LUFS, then a relative gate 10 LU under the mean of the blocks that passed."""
    n = x.shape[1]
    z1 = np.exp(-2j * np.pi * np.fft.rfftfreq(n, 1 / SR) / SR)
    bq = lambda b, a: (b[0] + b[1] * z1 + b[2] * z1 ** 2) / (a[0] + a[1] * z1 + a[2] * z1 ** 2)
    k = (bq((1.53512485958697, -2.69169618940638, 1.19839281085285), (1.0, -1.69065929318241, 0.73248077421585))
         * bq((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621)))
    c = np.cumsum(np.fft.irfft(np.fft.rfft(x, axis=1) * k, n=n, axis=1) ** 2, axis=1)
    c = np.concatenate([np.zeros((2, 1)), c], axis=1)
    blk, hop = int(0.4 * SR), int(0.1 * SR)
    s = np.arange(0, n - blk + 1, hop)
    z = ((c[:, s + blk] - c[:, s]) / blk).sum(axis=0)
    loud = lambda m: -0.691 + 10 * np.log10(np.maximum(m, 1e-20))
    z = z[loud(z) > -70]
    z = z[loud(z) > loud(z.mean()) - 10]
    return loud(z.mean())


# ── the master: TARGET LUFS integrated, and no sample above CEIL (-3.1 dBFS, room for the AAC encoder). A passage with
# a high crest ends quieter than TARGET, never louder. ──
TARGET, CEIL = -18.0, 0.7
out = np.tanh(1.3 * mix) / np.tanh(1.3)
out *= min(10 ** ((TARGET - lufs(out)) / 20), CEIL / np.max(np.abs(out)))
pcm = (np.clip(out.T, -1, 1) * 32767).astype('<i2')
with wave.open(sys.argv[2], 'wb') as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print(f'wrote {sys.argv[2]}: {DUR:.2f}s, {len(meta["events"])} events')
