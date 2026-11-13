import copy
import unittest
import lightmap as lm


class LightReportTests(unittest.TestCase):
    def test_counts_levels_and_rate(self):
        result = {'seconds': 60, 'tokens': [(0, 'P'), (0, 'B'), (1000, 'h'), (2000, 'H'), (3000, 'H'), (5000, 'Z')]}
        s = lm.hit_summary(result)
        self.assertEqual(3, s['hits']); self.assertEqual(1, s['level2_hits']); self.assertEqual(2, s['level3_hits'])
        self.assertEqual(3.0, s['hits_per_minute'])
        self.assertEqual((3000, 60000, 57000), (s['gap_start_ms'], s['gap_end_ms'], s['longest_gap_ms']))
        self.assertIn('3 mapped hits (3.0/min; 1 level 2, 2 level 3)', lm.hit_report(result))
        self.assertIn('00:03.0 to 01:00.0', lm.hit_report(result))
        self.assertEqual('01:00.0', lm._clock(59999), 'rounding a gap position carries into the next minute')

    def test_grid_is_not_reported_as_detected_hits(self):
        result = {'seconds': 20, 'tokens': [(1000, 'B'), (1500, 'c'), (10000, 'D')]}
        s = lm.hit_summary(result)
        self.assertEqual(0, s['hits']); self.assertEqual(20000, s['longest_gap_ms'])
        self.assertTrue(any('beat-grid fallback' in w for w in lm.hit_warnings(result)))
        self.assertTrue(any('intended intro, break or outro' in w for w in lm.hit_warnings(result)))

    def test_gap_includes_intro_middle_and_outro(self):
        cases = [(30000, [(10000, 'H'), (20000, 'h')], (0, 10000)),
                 (30000, [(1000, 'H'), (20000, 'h')], (1000, 20000)),
                 (30000, [(1000, 'H'), (2000, 'h')], (2000, 30000))]
        for seconds_ms, tokens, expected in cases:
            with self.subTest(tokens=tokens):
                s = lm.hit_summary({'seconds': seconds_ms / 1000, 'tokens': tokens})
                self.assertEqual(expected, (s['gap_start_ms'], s['gap_end_ms']))

    def test_diagnostics_do_not_change_detector_output(self):
        result = {'seconds': 12, 'tokens': [(0, 'H'), (2000, 'K'), (10000, 'D'), (10000, 'H'), (12000, 'h')]}
        before = copy.deepcopy(result)
        lm.hit_summary(result); lm.hit_report(result); lm.hit_warnings(result); lm.report_markdown({'s': result})
        self.assertEqual(before, result)
        self.assertEqual(3, lm.hit_summary(result)['hits'], 'hits exactly at the duration boundary count')

    def test_markdown_handles_missing_tracks_and_explains_limits(self):
        text = lm.report_markdown({'known': {'seconds': 60, 'tokens': [(1000, 'h')]}}, ['known', 'missing'])
        self.assertIn('| known | 1 | 1.0 | 1 / 0 | 59.0 s |', text)
        self.assertIn('| missing | unavailable |', text)
        self.assertIn('not measured flashes', text)
        self.assertIn('review this track by ear', text)
        self.assertIn('Visual timing and Java/Bedrock playback still need an in-game check', text)

    def test_quiet_gaps_are_review_notes_not_build_failures(self):
        result = {'seconds': 60, 'tokens': [(1000, 'H'), (59000, 'H')]}
        self.assertEqual(2, len(lm.hit_warnings(result)))
        self.assertIn('Review notes:', lm.report_markdown({'s': result}))
        self.assertEqual([], lm.hit_warnings({'seconds': 4, 'tokens': [(0, 'h'), (1000, 'H'), (2000, 'h'), (3000, 'H'), (4000, 'h')]}))

    def test_zero_duration_has_finite_diagnostics(self):
        s = lm.hit_summary({'seconds': 0, 'tokens': []})
        self.assertEqual(0.0, s['hits_per_minute']); self.assertEqual(0, s['longest_gap_ms'])


if __name__ == '__main__': unittest.main(verbosity=2)
