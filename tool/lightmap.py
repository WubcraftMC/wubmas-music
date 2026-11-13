#!/usr/bin/env python3
"""Light map: finds the beat, the bars and the breaks and drops of a DJ set, so the stage lights can follow the music.

  python lightmap.py FILE [FILE ...] [--out lights.txt]

Why it is done here and not on the server: the sets are recordings. Everything the lights need to know (where every beat is, where the music drops out
and where it comes back) can be worked out once, before the show, from the same audio that is played. The server then only reads a list of times and
runs a few commands at those times, on the same clock as the music: no audio work on the server, nothing to lag, and Java and Bedrock players see the
same thing. (What it cannot do is follow a DJ who plays live and unrecorded.)

It needs Python 3.8 or newer and ffmpeg, nothing else. ffmpeg splits the sound into three bands (kick and bass, middle, highs) and reduces each to a
level a hundred times a second; the rest is plain Python:
  1. an onset curve (where the sound suddenly gets louder, kicks above all);
  2. the tempo, from how that curve repeats, window by window, so a DJ mix that changes tempo is followed;
  3. a beat track by dynamic programming (Ellis, 2007): the chain of beats that lands on the most onsets and keeps closest to the tempo;
  4. every beat's time is then moved to the exact kick (to the millisecond) and given a level 0-3 (0 = no kick there, 3 = a hard one);
  5. breaks (8 beats or more without a kick) and drops (the kick coming back after one);
  6. bars (every 4 beats) and phrases (every 8 bars), counted from the start, from every break and drop, and from every place where the beat jumps
     (a new track mixed in);
  7. actual bass hits, independent of the tempo grid, spaced at least 400 ms apart for pulses (the grid still drives bars and phrases).

The result is a text file that WubmasMusic reads (format below). It is a plain list: nothing in it can run a command by itself.
"""
import array, math, operator, re, shutil, subprocess, sys
from bisect import bisect_left, bisect_right
from itertools import accumulate

FORMAT = 2                  # hit tokens need WubmasMusic 1.14.0 or newer; install the plugin before rebuilding/downloading maps
FPS = 100                    # analysis frames a second
MIN_BPM, MAX_BPM = 75.0, 175.0
PRIOR_BPM, PRIOR_OCT = 125.0, 0.7       # a weak preference for house-like tempos (the tempo is the most likely of the possible ones, never forced)
ALPHA = 100.0                # how strongly the beat track holds to the tempo (the cost of a beat that is 10% early or late is about 1 strong onset)
BREAK_BEATS = 8              # this many beats in a row without a kick is a break
LEVEL_EDGES = (0.12, 0.45, 0.75)   # beat strength against the strong beats of the set: below the first = no kick, then levels 1, 2, 3
JUMP = 0.15                  # a beat interval 15% off the local one starts a new bar count (a new track, a changed tempo)
REFINE_MS = 28               # a beat is moved at most this far to land on its kick
HIT_PEAK_GAP_MS = 150        # do not count the kick's ringing as another hit
HIT_GAP_MS = 400             # at most 2.5 hit pulses per second; never lower this
TOKEN = re.compile(r"(\d{1,9})([abcABCZPDKhH])")
LETTERS = "abcABCZPDKhH"


class AnalysisError(Exception):
    pass


def default_ffmpeg(args):
    p = shutil.which("ffmpeg")
    if not p:
        raise AnalysisError("ffmpeg was not found")
    r = subprocess.run([p, "-hide_banner", "-nostdin", "-v", "error"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")


# ------------------------------------------------------------------ the three bands
def _band(ffmpeg, src, af, rate):
    rc, out, err = ffmpeg(["-i", str(src), "-vn", "-af", af, "-f", "f32le", "-ac", "1", "-ar", str(rate), "-"])
    if rc != 0:
        raise AnalysisError(f"ffmpeg could not read the audio:\n{err.strip()[-400:]}")
    a = array.array("f")
    a.frombytes(out[: len(out) // 4 * 4])
    if sys.byteorder == "big": a.byteswap()
    if len(a) < rate * 3: raise AnalysisError("the audio is shorter than 3 seconds")
    return a


def read_bands(ffmpeg, src):
    """(low at 1000 Hz: the sound below 150 Hz as it is; mid, high: their loudness 100 times a second)."""
    mono = "pan=mono|c0=0.5*c0+0.5*c1"
    low = _band(ffmpeg, src, f"{mono},lowpass=f=150,lowpass=f=150,aresample=1000", 1000)
    mid = _band(ffmpeg, src, f"{mono},aresample=8000,bandpass=f=1500:width_type=h:w=2500,aeval=abs(val(0)):c=same,lowpass=f=40,aresample={FPS}", FPS)
    high = _band(ffmpeg, src, f"{mono},aresample=16000,highpass=f=4000,aeval=abs(val(0)):c=same,lowpass=f=40,aresample={FPS}", FPS)
    return low, mid, high


# ------------------------------------------------------------------ small helpers
def _pct(values, q):
    if not values: return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(q * (len(s) - 1) + 0.5)))]


def _rms(x, win, hop):
    cs = [0.0]
    cs.extend(accumulate(v * v for v in x))
    n = (len(x) - win) // hop + 1
    inv = 1.0 / win
    return [math.sqrt(max(0.0, cs[i * hop + win] - cs[i * hop]) * inv) for i in range(max(0, n))]


def _flux(env, lag):
    env = [v if v > 0.0 else 0.0 for v in env]      # a filter can ring a little below zero
    ref = _pct(env, 0.90)
    k = 1.0 / (0.05 * ref) if ref > 1e-9 else 0.0
    c = [math.log1p(v * k) for v in env]
    return [0.0] * lag + [max(0.0, c[i] - c[i - lag]) for i in range(lag, len(c))]


def _norm(v):
    p = _pct(v, 0.95)
    return [min(2.0, x / p) for x in v] if p > 1e-9 else [0.0] * len(v)


def onset_curve(low, mid, high):
    """The onset curve at FPS, and the fine kick curve at 1000 Hz (for the exact time of a kick)."""
    el = _rms(low, 20, 10)
    n = min(len(el), len(mid), len(high))
    if n < FPS * 3: raise AnalysisError("the audio is shorter than 3 seconds")
    ol = _norm(_flux(el[:n], 2))
    om = _norm(_flux(list(mid[:n]), 1))
    oh = _norm(_flux(list(high[:n]), 1))
    onset = [a + 0.5 * b + 0.25 * c for a, b, c in zip(ol, om, oh)]
    e5 = _rms(low, 5, 1)
    fine = [0.0] * 3 + [max(0.0, e5[i] - e5[i - 3]) for i in range(3, len(e5))]
    return onset, el[:n], fine


# ------------------------------------------------------------------ tempo
def tempo_track(onset):
    """The beat period in frames at every frame: the strongest repeat of the onset curve in an 8 second window, every 2 seconds, smoothed over 3 windows."""
    W, H = 8 * FPS, 2 * FPS
    lo, hi = int(FPS * 60.0 / MAX_BPM), int(math.ceil(FPS * 60.0 / MIN_BPM))
    n = len(onset)
    centers, periods = [], []
    for s in range(0, max(1, n - W // 2), H):
        seg = onset[s:s + W]
        if len(seg) < W // 2: break
        m = sum(seg) / len(seg)
        seg = [v - m for v in seg]
        if max(seg) - min(seg) < 1e-6: periods.append(None); centers.append(s + len(seg) // 2); continue
        acf = {}
        for L in range(lo - 1, 2 * hi + 2):
            if L >= len(seg) - 4: break
            acf[L] = sum(map(operator.mul, seg[:-L], seg[L:])) / (len(seg) - L)
        best, bl = None, None
        for L in range(lo, hi + 1):
            if L not in acf: continue
            bpm = FPS * 60.0 / L
            prior = math.exp(-0.5 * (math.log2(bpm / PRIOR_BPM) / PRIOR_OCT) ** 2)
            v = (acf[L] + 0.5 * acf.get(2 * L, 0.0)) * (0.5 + 0.5 * prior)
            if best is None or v > best: best, bl = v, L
        if bl is None or best <= 0: periods.append(None)
        else:
            a, b, c = acf.get(bl - 1, acf[bl]), acf[bl], acf.get(bl + 1, acf[bl])
            den = a - 2 * b + c
            off = 0.5 * (a - c) / den if den < -1e-12 else 0.0
            periods.append(bl + max(-0.5, min(0.5, off)))
        centers.append(s + len(seg) // 2)
    known = [p for p in periods if p]
    if not known: raise AnalysisError("no steady beat was found")
    fill = known[0]
    for i, p in enumerate(periods):
        if p is None: periods[i] = fill
        else: fill = p
    sm = [sorted(periods[max(0, i - 1):i + 2])[len(periods[max(0, i - 1):i + 2]) // 2] for i in range(len(periods))]
    out, j = [], 0
    for t in range(n):
        while j + 1 < len(centers) and abs(centers[j + 1] - t) <= abs(centers[j] - t): j += 1
        out.append(sm[j])
    return out


# ------------------------------------------------------------------ beat track (dynamic programming)
def beat_track(onset, period):
    n = len(onset)
    C = [0.0] * n
    back = [-1] * n
    cache = {}
    sub = operator.sub
    for t in range(n):
        key = int(period[t] * 4 + 0.5)
        got = cache.get(key)
        if got is None:
            P = key / 4.0
            tlo = max(1, int(0.5 * P))
            got = (tlo, [ALPHA * math.log(tau / P) ** 2 for tau in range(tlo, int(2 * P) + 2)])
            cache[key] = got
        tlo, pen = got
        start = t - tlo
        best, bi = 0.0, -1
        if start >= 0:
            stop = start - len(pen)
            seg = C[start:stop:-1] if stop >= 0 else C[start::-1]
            vals = list(map(sub, seg, pen))
            if vals:
                m = max(vals)
                if m > 0.0: best, bi = m, start - vals.index(m)
        C[t] = onset[t] + best
        back[t] = bi
    tail = int(2 * period[-1]) + 2
    end = max(range(max(0, n - tail), n), key=C.__getitem__)
    beats = []
    while end >= 0:
        beats.append(end)
        end = back[end]
    beats.reverse()
    return beats


def refine(beats, fine, spread_ms=REFINE_MS):
    """Beat frames (10 ms) to milliseconds, each moved to the sharpest rise of the kick band within +-spread_ms, if that rise stands out of its surroundings
    (in a wobbling bass line it does not, and then the beat stays where the beat track put it); and the size of the rise."""
    step = 1000 // FPS
    out = []
    for f in beats:
        c = f * step - 5          # (measured on tracks with known kicks: the beat track's frame is about 15 ms after the kick)
        a, b = max(0, c - spread_ms), min(len(fine) - 1, c + spread_ms)
        if b <= a: out.append((c, 0.0)); continue
        w = fine[a:b + 1]
        m = max(w)
        med = sorted(w)[len(w) // 2]
        out.append((a + w.index(m) + 1, m) if m >= 3.0 * med and m > 0 else (c, m))
    return out


# ------------------------------------------------------------------ real bass hits (not the tempo grid)
def hit_peaks(fine):
    """(ms, relative strength) of distinct rises in the 1 ms kick curve.
    Use the same peak-index + 1 ms time estimator as refine(). Peaks must stand
    3x above their local median (+-100 ms); the strongest wins within 150 ms.
    Strength is relative to the 85th percentile of those distinct peaks.
    """
    peaks = []
    for i in range(1, len(fine) - 1):
        s = fine[i]
        if s <= 0 or s <= fine[i - 1] or s < fine[i + 1]: continue
        window = sorted(fine[max(0, i - 100):i + 101])
        if s >= 3.0 * window[len(window) // 2]: peaks.append((i + 1, s))
    # Non-maximum suppression, in strength order. Exactly 150 ms apart is allowed.
    chosen, times = [], []
    for t, s in sorted(peaks, key=lambda x: (-x[1], x[0])):
        j = bisect_left(times, t)
        if j and t - times[j - 1] < HIT_PEAK_GAP_MS: continue
        if j < len(times) and times[j] - t < HIT_PEAK_GAP_MS: continue
        times.insert(j, t); chosen.append((t, s))
    ref = _pct([s for _, s in chosen], 0.85)
    if ref <= 1e-9: return []
    return sorted((t, s / ref) for t, s in chosen if s / ref >= LEVEL_EDGES[1])


def thin_hits(hits, gap_ms=HIT_GAP_MS):
    """Weighted-interval scheduling: keep the greatest total strength with
    at least gap_ms between hits, rather than greedily taking the first hit.
    The 400 ms floor also applies if a caller supplies a smaller gap.
    """
    gap_ms = max(HIT_GAP_MS, gap_ms)
    hits = sorted(hits)
    times = [t for t, _ in hits]
    best, prev, take = [0.0] * (len(hits) + 1), [], []
    for i, (t, s) in enumerate(hits):
        p = bisect_right(times, t - gap_ms, 0, i)
        use = best[p] + s
        prev.append(p); take.append(use > best[i])
        best[i + 1] = use if take[-1] else best[i]
    out, i = [], len(hits)
    while i:
        if take[i - 1]: out.append(hits[i - 1]); i = prev[i - 1]
        else: i -= 1
    return list(reversed(out))


def build_hits(fine, grid_tokens):
    """Hit tokens, outside [break, drop) intervals, then safely spaced.
    Filter breaks before scheduling so a discarded hit cannot crowd out one
    just before a break or just after a drop. An unclosed break lasts to EOF.
    """
    structural = [(t, k) for t, k in grid_tokens if k in "KD"]
    candidates, j, inside = [], 0, False
    for t, s in hit_peaks(fine):
        while j < len(structural) and structural[j][0] <= t:
            inside = structural[j][1] == "K"; j += 1
        if not inside: candidates.append((t, s))
    return [(t, "H" if s >= LEVEL_EDGES[2] else "h") for t, s in thin_hits(candidates)]


# ------------------------------------------------------------------ levels, breaks, drops
def levels(ref_beats):
    """Level 0-3 of every beat, from the size of its kick against the strong beats of the set."""
    strengths = [s for _, s in ref_beats]
    ref = _pct(strengths, 0.85)
    if ref <= 1e-9: return [0] * len(strengths)
    out = []
    for s in strengths:
        r = s / ref
        out.append(0 if r < LEVEL_EDGES[0] else 1 if r < LEVEL_EDGES[1] else 2 if r < LEVEL_EDGES[2] else 3)
    return out


def sections(lv):
    """Indices of beats where a break starts and where a drop comes (the kick returns after a break)."""
    breaks, drops = [], []
    n = len(lv)
    state = "unknown"
    i = 0
    run_start = None
    while i < n:
        if state in ("unknown", "low"):
            if lv[i] >= 2 and sum(1 for j in range(i, min(n, i + 4)) if lv[j] >= 2) >= min(3, n - i):
                if state == "low": drops.append(i)
                state = "high"
        else:
            # high: a run of BREAK_BEATS beats with no kick is a break that began at the first of them
            if lv[i] <= 1:
                j = i
                while j < n and lv[j] <= 1: j += 1
                if j - i >= BREAK_BEATS or (j == n and j - i >= BREAK_BEATS):
                    breaks.append(i)
                    state = "low"
                i = max(i + 1, j) if state == "low" else i + 1
                continue
        i += 1
    return breaks, drops


def energy_events(times, el, med_gap):
    """Breaks and drops from the bass: a break where the low band's energy falls to half of what it was over the last 12 beats, a drop where it is 1.6 times that.
    This finds the drop after a build, where the kick never stopped. el: the low band's loudness 100 times a second."""
    n = len(times)
    if n < 20 or not el: return [], []
    cs = [0.0]
    cs.extend(accumulate(el))
    def mean(a_ms, b_ms):
        fa = min(len(el) - 1, max(0, a_ms // 10)); fb = min(len(el), max(fa + 1, b_ms // 10))
        return (cs[fb] - cs[fa]) / (fb - fa)
    en = [mean(times[i], times[i + 1] if i + 1 < n else times[i] + med_gap) for i in range(n)]
    ref = _pct(en, 0.90)
    if ref <= 1e-9: return [], []
    breaks, drops = [], []
    last_d = last_k = -99
    for i in range(12, n - 3):
        p = sum(en[i - 12:i]) / 12.0
        f = sum(en[i:i + 4]) / 4.0
        if p <= 1e-9: continue
        r = f / p
        if r >= 1.6 and f >= 0.4 * ref and i - last_d >= 16:
            j = max(range(i, min(n - 1, i + 4)), key=lambda k: sum(en[k:k + 2]) - sum(en[k - 2:k]))
            drops.append(j); last_d = j
        elif r <= 0.5 and p >= 0.4 * ref and i - last_k >= 16:
            j = min(range(i, min(n - 1, i + 4)), key=lambda k: sum(en[k:k + 2]) - sum(en[k - 2:k]))
            breaks.append(j); last_k = j
    return breaks, drops


def merge_events(a, b, gap=8):
    """Two lists of beat indices into one; one that is within `gap` beats after another is the same event."""
    out = []
    for i in sorted(set(a) | set(b)):
        if out and i - out[-1] < gap: continue
        out.append(i)
    return out


def thin_events(breaks, drops):
    """Breaks and drops in order, with the stutters taken out: a drop must be 8 beats or more after the break it ends, a break 16 beats or more after the drop before it,
    and two of the same kind in a row are one (the first). (A bass line that drops out every bar is not a string of breakdowns.)"""
    ev = sorted([(i, "K") for i in breaks] + [(i, "D") for i in drops])
    out = []
    for i, k in ev:
        if out:
            j, pk = out[-1]
            if pk == k: continue
            if i - j < (8 if pk == "K" else 16): continue
        out.append((i, k))
    return [i for i, k in out if k == "K"], [i for i, k in out if k == "D"]


def build_tokens(times, lv, breaks, drops, jumps, quiet=frozenset()):
    """The cue list: (ms, letter) in playing order. Beats with level 0 are left out, except the first beat of a bar (Z). `quiet`: beats where no phrase starts (a break)."""
    anchors = set(breaks) | set(drops) | set(jumps)
    first = next((i for i, v in enumerate(lv) if v >= 1), None)
    if first is None: return []
    anchors.add(first)
    ev = []
    last = first
    bset, dset = set(breaks), set(drops)
    for i in range(first, len(times)):
        if i in anchors: last = i
        k = i - last
        t = times[i]
        if i in dset: ev.append((t, 0, "D"))
        if i in bset: ev.append((t, 0, "K"))
        down = k % 4 == 0
        if down and (k // 4) % 8 == 0 and i not in bset and i not in quiet: ev.append((t, 1, "P"))
        v = 0 if i in quiet else lv[i]
        if down: ev.append((t, 2, "ZABC"[v]))
        elif v >= 1: ev.append((t, 2, "abc"[v - 1]))
    ev.sort(key=lambda e: (e[0], e[1]))
    return [(t, c) for t, _, c in ev]


def periodicity(onset):
    """How regularly the onset curve repeats: the strength of its best repeat at a tempo in range, 0 to 1, taken over 8 second windows (the middle one of them).
    Noise and speech are under 0.09, a song with a beat is 0.11 or more, a clean electronic beat 0.3 or more."""
    W, H = 8 * FPS, 4 * FPS
    lo, hi = int(FPS * 60.0 / MAX_BPM), int(math.ceil(FPS * 60.0 / MIN_BPM))
    vals = []
    for s in range(0, max(1, len(onset) - W), H):
        seg = onset[s:s + W]
        if len(seg) < W // 2: break
        m = sum(seg) / len(seg)
        seg = [v - m for v in seg]
        v0 = sum(map(operator.mul, seg, seg))
        if v0 < 1e-9: continue
        vals.append(max(sum(map(operator.mul, seg[:-L], seg[L:])) for L in range(lo, min(hi, len(seg) - 2) + 1)) / v0)
    vals.sort()
    return vals[len(vals) // 2] if vals else 0.0


def analyze(src, ffmpeg=default_ffmpeg):
    """Analyses one audio file. Returns a dict: tokens [(ms, letter)], bpm, seconds, beats, drops, breaks, steady (0-1) and warnings [text]."""
    low, mid, high = read_bands(ffmpeg, src)
    seconds = len(low) / 1000.0
    onset, el, fine = onset_curve(low, mid, high)
    warnings = []
    try:
        period = tempo_track(onset)
        frames = beat_track(onset, period)
    except AnalysisError as e:
        return {"tokens": [], "bpm": 0.0, "seconds": seconds, "beats": 0, "drops": 0, "breaks": 0, "steady": 0.0, "warnings": [str(e)]}
    ref = refine(frames, fine)
    times = [t for t, _ in ref]
    # the refined times must stay in order and apart (two beats can never be 100 ms from each other at 175 BPM or slower)
    keep = [0]
    for i in range(1, len(times)):
        if times[i] - times[keep[-1]] >= 200: keep.append(i)
    ref = [ref[i] for i in keep]
    times = [t for t, _ in ref]
    lv = levels(ref)
    gaps = [b - a for a, b in zip(times, times[1:])]
    if len(gaps) < 8:
        return {"tokens": [], "bpm": 0.0, "seconds": seconds, "beats": len(times), "drops": 0, "breaks": 0, "steady": 0.0, "warnings": ["no steady beat was found"]}
    med = sorted(gaps)[len(gaps) // 2]
    breaks_a, drops_a = sections(lv)
    breaks_b, drops_b = energy_events(times, el, med)
    breaks, drops = thin_events(merge_events(breaks_a, breaks_b), merge_events(drops_a, drops_b))
    inside, last_k = set(), None       # the beats of a break are not a rhythm to follow: no new bar count starts in one
    for i, k in sorted([(i, "K") for i in breaks] + [(i, "D") for i in drops]):
        if k == "K": last_k = i
        elif last_k is not None: inside.update(range(last_k, i)); last_k = None
    if last_k is not None: inside.update(range(last_k, len(times)))
    jumps = []
    for i in range(8, len(times)):
        local = sorted(gaps[i - 8:i - 1])[3]
        if abs(gaps[i - 1] - local) > JUMP * local and lv[i] >= 2 and lv[i - 1] >= 2 and i not in inside: jumps.append(i)
    tokens = build_tokens(times, lv, breaks, drops, jumps, inside)
    # Stable sort: structural cues, phrases and grid bars precede a hit at the
    # same time, so holds and the bar's current colour are already in place.
    tokens = sorted(tokens + build_hits(fine, tokens), key=lambda x: x[0])
    steady = periodicity(onset)
    kicks = sum(1 for v in lv if v >= 2)
    if steady < 0.09 and not warnings: warnings.append("no steady beat in this one (it may be speech or noise): the lights may drift against the music")
    elif kicks < max(16, seconds / 60.0 * 20): warnings.append(f"only {kicks} clear beats in {seconds / 60:.1f} minutes: the lights will mostly stay still")
    return {"tokens": tokens, "bpm": round(60000.0 / med, 1), "seconds": seconds, "beats": len(times), "drops": len(drops), "breaks": len(breaks),
            "steady": round(steady, 3), "warnings": warnings, "times": times, "levels": lv}


# ------------------------------------------------------------------ build diagnostics (mapped hits, not rendered flashes)
def hit_summary(result):
    """Counts only the emitted h/H stream; gap includes intro, breaks and outro.
    Server holds, strength thresholds, TPS and cue budgets can reduce actual pulses.
    These diagnostics never modify detection or map tokens.
    """
    end = max(0, int(round(float(result['seconds']) * 1000)))
    hits = sorted((int(t), k) for t, k in result['tokens'] if k in ('h', 'H') and 0 <= t <= end)
    times = [0] + [t for t, _ in hits] + [end]
    start, finish = max(zip(times, times[1:]), key=lambda pair: pair[1] - pair[0])
    return {'hits': len(hits), 'level2_hits': sum(k == 'h' for _, k in hits), 'level3_hits': sum(k == 'H' for _, k in hits),
            'hits_per_minute': round(len(hits) * 60000 / end, 1) if end else 0.0,
            'longest_gap_ms': finish - start, 'gap_start_ms': start, 'gap_end_ms': finish}


def hit_warnings(result):
    s = hit_summary(result)
    warnings = []
    if not s['hits']: warnings.append('No detected-hit cues; this track uses beat-grid fallback.')
    elif s['hits_per_minute'] < 20: warnings.append(f"Sparse mapped hits ({s['hits_per_minute']:.1f}/min); review this track by ear.")
    if s['longest_gap_ms'] >= 10000:
        warnings.append(f"{s['longest_gap_ms'] / 1000:.1f} s gap without mapped hits; check whether this is an intended intro, break or outro.")
    return warnings


def _clock(ms):
    tenths = int(round(ms / 100))
    return f'{tenths // 600:02d}:{tenths % 600 / 10:04.1f}'


def hit_report(result):
    s = hit_summary(result)
    return (f"{s['hits']} mapped hits ({s['hits_per_minute']:.1f}/min; {s['level2_hits']} level 2, {s['level3_hits']} level 3); "
            f"longest gap {s['longest_gap_ms'] / 1000:.1f} s ({_clock(s['gap_start_ms'])} to {_clock(s['gap_end_ms'])})")


def report_markdown(results, set_ids=None):
    """A complete per-track report for both normal and lights-only builds."""
    lines = ['## Per-song light report', '',
             'Counts are mapped hit cues, not measured flashes. Server holds, level thresholds and lag protection can reduce pulses. Longest gaps include intros, breaks and outros.', '',
             '| Set | Mapped hits | Hits/min | Level 2 / 3 | Longest gap | Gap position |',
             '| --- | ---: | ---: | ---: | ---: | --- |']
    notes = []
    for sid in (set_ids if set_ids is not None else results):
        result = results.get(sid)
        label = str(sid).replace('|', '\\|').replace('\n', ' ')
        if result is None:
            lines.append(f'| {label} | unavailable | — | — | — | No usable light map |')
            notes.append(f'{label}: no usable light map; lights cannot follow this set.')
            continue
        s = hit_summary(result)
        lines.append(f"| {label} | {s['hits']} | {s['hits_per_minute']:.1f} | {s['level2_hits']} / {s['level3_hits']} | {s['longest_gap_ms'] / 1000:.1f} s | {_clock(s['gap_start_ms'])}–{_clock(s['gap_end_ms'])} |")
        notes.extend(f'{label}: {w}' for w in hit_warnings(result))
    if notes: lines += ['', 'Review notes:', ''] + ['- ' + note for note in notes]
    lines += ['', 'This is offline analysis. Visual timing and Java/Bedrock playback still need an in-game check.']
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ the file
def format_set(sid, res):
    """One set's lines of the file: `set ID SECONDS BPM`, then the tokens, 20 to a line. A token is the milliseconds since the one before (the first: since 0) and a letter:
    a b c = a beat of level 1 2 3; A B C = the first beat of a bar, level 1 2 3; Z = the first beat of a bar with no kick; P = the first bar of a phrase (just before its
    A B C Z); D = the kick comes back after a break; K = a break begins; h H = a detected bass hit of level 2 3 (not a bar)."""
    lines = [f"set {sid} {res['seconds']:.1f} {res['bpm']:.1f}"]
    prev = 0
    toks = []
    for t, c in res["tokens"]:
        toks.append(f"{max(0, int(t) - prev)}{c}")
        prev = int(t)
    for i in range(0, len(toks), 20):
        lines.append("t " + " ".join(toks[i:i + 20]))
    return lines


def format_map(results):
    """results: {set id: analyze() result}. The whole file."""
    L = [f"wubmas-lights {FORMAT}", "# Written by the Wubmas music builder (lightmap.py). Times are milliseconds from the start of the set. Do not edit."]
    for sid in results:
        L += format_set(sid, results[sid])
    return "\n".join(L) + "\n"


def parse_map(text):
    """The file back into {set id: {"seconds", "bpm", "tokens": [(ms, letter)]}}; for tests and checks. Raises ValueError on anything it does not understand."""
    out, cur, seen = {}, None, False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"): continue
        p = line.split()
        if not seen:
            if len(p) != 2 or p[0] != "wubmas-lights" or p[1] not in ("1", str(FORMAT)): raise ValueError(f"line {n}: not a light map (it must start with 'wubmas-lights 1' or 'wubmas-lights {FORMAT}')")
            seen = True; continue
        if p[0] == "set" and len(p) == 4:
            cur = {"seconds": float(p[2]), "bpm": float(p[3]), "tokens": [], "_t": 0}
            if p[1] in out: raise ValueError(f"line {n}: set {p[1]} twice")
            out[p[1]] = cur
        elif p[0] == "t" and cur is not None:
            for tok in p[1:]:
                m = TOKEN.fullmatch(tok)
                if not m: raise ValueError(f"line {n}: bad token {tok!r}")
                cur["_t"] += int(m.group(1))
                cur["tokens"].append((cur["_t"], m.group(2)))
        else:
            raise ValueError(f"line {n}: unexpected {line[:40]!r}")
    if not seen: raise ValueError("empty file")
    for v in out.values(): v.pop("_t")
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Finds the beat, bars, breaks and drops of audio files and writes a light map.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="lights.txt", help="the light map to write (default lights.txt)")
    a = ap.parse_args(argv)
    results = {}
    for f in a.files:
        sid = re.sub(r"[^a-z0-9_]+", "_", re.sub(r"\.[^.]*$", "", f.replace("\\", "/").split("/")[-1]).lower()).strip("_") or "set"
        try:
            r = analyze(f)
        except AnalysisError as e:
            print(f"{f}: {e}"); return 1
        results[sid] = r
        print(f"{sid}: {r['bpm']} BPM, {r['beats']} beats, {r['breaks']} breaks, {r['drops']} drops, {r['steady'] * 100:.0f}% steady" + "".join(f"\n  warning: {w}" for w in r["warnings"]))
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(format_map(results))
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
