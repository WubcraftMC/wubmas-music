#!/usr/bin/env python3
"""Run: python3 -m unittest discover -s tool -p test_lightmap_hits.py -v
Requires ffmpeg and numpy for the full audio-to-map synthetic regressions.
"""
import itertools
import random
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import lightmap as lm


class HitTests(unittest.TestCase):
    def test_peak_time_and_level_cutoff(self):
        fine = [0.0] * 3000
        for t, s in [(300, 1.0), (800, 0.6), (1300, 0.44), (1800, 0.75), (2300, 1.0)]:
            fine[t - 1] = s
        self.assertEqual([(300, 1.0), (800, 0.6), (1800, 0.75), (2300, 1.0)], lm.hit_peaks(fine))
        self.assertEqual([(300, 'H'), (800, 'h'), (1800, 'H'), (2300, 'H')], lm.build_hits(fine, []))

    def test_peak_prominence_and_silence(self):
        self.assertEqual([], lm.hit_peaks([]))
        self.assertEqual([], lm.hit_peaks([0.0] * 1000))
        fine = [1.0] * 1000
        fine[300] = 2.99
        self.assertEqual([], lm.hit_peaks(fine))
        fine[300] = 3.0
        self.assertEqual([(301, 1.0)], lm.hit_peaks(fine))

    def test_peak_ringing_and_exact_150ms_boundary(self):
        fine = [0.0] * 1500
        for i, s in [(300, 0.9), (350, 1.0), (499, 0.8), (500, 0.7), (900, 1.0)]: fine[i] = s
        self.assertEqual([351, 901], [t for t, _ in lm.hit_peaks(fine)])
        fine[499] = 0.0
        self.assertEqual([351, 501, 901], [t for t, _ in lm.hit_peaks(fine)])

    def test_plateau_uses_first_index_like_refine(self):
        fine = [0.0] * 1000
        fine[300:303] = [1.0] * 3
        self.assertEqual([(301, 1.0)], lm.hit_peaks(fine))

    def test_weighted_schedule_beats_both_greedy_choices(self):
        self.assertEqual([(300, 3.0)], lm.thin_hits([(0, 1.0), (300, 3.0), (600, 1.0)]))
        self.assertEqual([(0, 2.0), (600, 2.0)], lm.thin_hits([(0, 2.0), (300, 3.0), (600, 2.0)]))
        self.assertEqual([], lm.thin_hits([]))

    def test_gap_floor_and_custom_longer_gap(self):
        hits = [(0, 1.0), (399, 1.0), (400, 1.0), (800, 1.0)]
        for gap in [0, 100, 300, 400]:
            with self.subTest(gap=gap): self.assertEqual([0, 400, 800], [t for t, _ in lm.thin_hits(hits, gap)])
        self.assertEqual([0, 800], [t for t, _ in lm.thin_hits(hits, 700)])

    def test_schedule_matches_exhaustive_optimum(self):
        rng = random.Random(1414)
        for trial in range(80):
            hits = sorted((t, rng.randint(1, 10)) for t in rng.sample(range(0, 2200), 9))
            best = max(sum(s for _, s in subset) for n in range(10) for subset in itertools.combinations(hits, n)
                       if all(b[0] - a[0] >= 400 for a, b in zip(subset, subset[1:])))
            chosen = lm.thin_hits(list(reversed(hits)))
            with self.subTest(trial=trial):
                self.assertEqual(best, sum(s for _, s in chosen))
                self.assertTrue(all(b[0] - a[0] >= 400 for a, b in zip(chosen, chosen[1:])))

    def test_break_boundaries_and_unclosed_break(self):
        hits = [(900, 1.0), (1000, 1.0), (1500, 1.0), (1999, 1.0), (2000, 1.0), (2500, 1.0), (3000, 1.0), (3500, 1.0)]
        with patch.object(lm, 'hit_peaks', return_value=hits):
            self.assertEqual([(900, 'H'), (2000, 'H'), (2500, 'H')],
                             lm.build_hits([], [(1000, 'K'), (2000, 'D'), (3000, 'K')]))

    def test_break_filter_precedes_scheduling(self):
        with patch.object(lm, 'hit_peaks', return_value=[(900, 0.6), (1000, 2.0), (2000, 0.6)]):
            self.assertEqual([(900, 'h'), (2000, 'h')], lm.build_hits([], [(1000, 'K'), (2000, 'D')]))

    def test_map_roundtrip_old_and_new(self):
        results = {'test': {'seconds': 10.0, 'bpm': 120.0, 'tokens': [(1000, 'D'), (1000, 'P'), (1000, 'B'), (1000, 'H'), (1500, 'h')]}}
        text = lm.format_map(results)
        self.assertTrue(text.startswith('wubmas-lights 2\n'))
        self.assertIn('1000D 0P 0B 0H 500h', text)
        self.assertEqual(results, lm.parse_map(text))
        self.assertEqual([(1000, 'B')], lm.parse_map('wubmas-lights 1\nset old 10 120\nt 1000B\n')['old']['tokens'])
        for text in ['wubmas-lights 3\n', 'wubmas-lights 2\nset bad 10 120\nt 100q\n']:
            with self.assertRaises(ValueError): lm.parse_map(text)


class SyntheticAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='wubmas-hits-')
        sr, seconds = 44100, 90
        t = np.arange(sr * seconds) / sr
        out = np.zeros(len(t))
        beat = 60 / 140
        cls.truth = []
        for k in range(int(seconds / beat)):
            start = k * beat + 0.5
            if 40 < start < 52: continue
            i, n = round(start * sr), int(0.2 * sr)
            tt = np.arange(n) / sr
            if i + n >= len(out): break
            out[i:i+n] += 0.9 * np.sin(2*np.pi*(55*tt + 85*0.03*(1-np.exp(-tt/0.03)))) * np.exp(-tt*14)
            cls.truth.append(i / sr * 1000)
            h = i + int(beat * sr / 2)
            if h + 800 < len(out): out[h:h+800] += 0.12 * np.random.default_rng(k).standard_normal(800) * np.exp(-np.arange(800)/150)
        out += 0.10 * np.sin(2*np.pi*55*t) * (0.5 + 0.5*np.sin(2*np.pi*t/(beat*2)))
        out /= np.max(np.abs(out)) * 1.1
        cls.path = Path(cls.tmp.name) / 'known-kicks.wav'
        with wave.open(str(cls.path), 'wb') as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes((np.stack([out, out], axis=1) * 32767).astype('<i2').tobytes())
        low, mid, high = lm.read_bands(lm.default_ffmpeg, cls.path)
        _, _, cls.fine = lm.onset_curve(low, mid, high)
        cls.result = lm.analyze(cls.path)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_all_181_known_kicks_have_a_real_peak_within_4ms(self):
        peaks = lm.hit_peaks(self.fine)
        self.assertEqual(181, len(self.truth))
        self.assertEqual(len(self.truth), len(peaks), 'no extra bass-line or hi-hat detections')
        for true in self.truth:
            with self.subTest(kick_ms=true): self.assertLessEqual(min(abs(t - true) for t, _ in peaks), 4.0)

    def test_full_analyzer_emits_safe_hits_on_true_kicks(self):
        hits = [(t, k) for t, k in self.result['tokens'] if k in 'hH']
        self.assertGreater(len(hits), 170, 'synthetic steady kicks should survive scheduling')
        for t, _ in hits:
            with self.subTest(hit_ms=t):
                self.assertLessEqual(min(abs(t - true) for true in self.truth), 4.0)
                self.assertFalse(40000 <= t < 52000, 'no hits in the known break')
        self.assertTrue(all(b[0] - a[0] >= 400 for a, b in zip(hits, hits[1:])))

    def test_no_hits_in_any_detected_break(self):
        inside = False
        for t, k in self.result['tokens']:
            if k == 'K': inside = True
            elif k == 'D': inside = False
            elif k in 'hH': self.assertFalse(inside, f'hit at {t} in a detected break')

    def test_stable_order_and_roundtrip_full_map(self):
        tokens = self.result['tokens']
        self.assertEqual(sorted(tokens, key=lambda x: x[0]), tokens)
        for i, (t, k) in enumerate(tokens):
            if k in 'hH': self.assertFalse(any(tt == t and kk in 'KDPabcABCZ' for tt, kk in tokens[i+1:i+5]))
        self.assertEqual(tokens, lm.parse_map(lm.format_map({'synth': self.result}))['synth']['tokens'])

    def test_syncopated_hits_do_not_snap_to_grid(self):
        # The 1 ms curve is exactly the builder's hit input: deliberately off a 500 ms grid.
        fine = [0.0] * 10000
        truth = [113, 537, 1191, 1600, 2247, 2733, 3239, 3701, 4319, 4877, 5399, 5903, 6501, 6999, 7403, 8007, 8519, 8991]
        for t in truth: fine[t-1] = 1.0
        hits = lm.build_hits(fine, [(t, 'B') for t in range(0, 10000, 2000)])
        self.assertEqual(truth, [t for t, _ in hits])


if __name__ == '__main__': unittest.main(verbosity=2)
