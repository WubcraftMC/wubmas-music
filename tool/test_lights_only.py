import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
import wave
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml
import lightmap
import make_lights_only as lights
import make_music_packs as music


class LightsOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.sets = self.root / 'sets'; self.sets.mkdir()
        self.audio = self.sets / 'song.wav'; self.audio.write_bytes(b'original source bytes')
        self.library = self.root / 'library.yml'; self.manifest = self.root / 'light-sources.json'; self.out = self.root / 'out'
        self.text = '''# Keep this comment and the published formatting.
build: 8
built: "2026-11-12"
piece-seconds: 2
overlap-seconds: 0.3
long-piece-seconds: 120
bedrock-long-piece-seconds: 30
resource-pack:
  url: "https://github.com/WubcraftMC/wubmas-music/releases/download/music-8/WubmasMusic-java.zip"
  sha1: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  layout: stereo
bedrock-pack:
  url: "https://github.com/WubcraftMC/wubmas-music/releases/download/music-8/WubmasMusic-bedrock.mcpack"
  sha1: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  bytes: 100000000
lights:
  url: "https://github.com/WubcraftMC/wubmas-music/releases/download/music-8/lights.txt"
  sha1: "cccccccccccccccccccccccccccccccccccccccc"
  bytes: 100
sets:
  - {id: song, name: "Test: Song", start: "2099-01-01 00:00", pieces: 8, seconds: 16}
'''
        self.lib = yaml.safe_load(self.text)
        self.meta = {'format': 1, 'normalize': False, 'lufs': -14, 'tp': -2,
                     'java-sha1': self.lib['resource-pack']['sha1'], 'bedrock-sha1': self.lib['bedrock-pack']['sha1'],
                     'sets': {'song': {'file': 'song.wav', 'sha256': lights.digest(self.audio), 'seconds': 16}}}
        self.library.write_text(self.text); self.manifest.write_text(json.dumps(self.meta))

    def build(self):
        return lights.build(self.sets, self.library, self.manifest, self.out, 'WubcraftMC/wubmas-music', 'music-8', 'lights-900-1.txt')

    def fake_analysis(self):
        return patch.object(music, 'analyse_lights', return_value=({'seconds': 16, 'bpm': 120, 'tokens': [(1000, 'P'), (1000, 'B'), (1000, 'H'), (1500, 'h')]}, []))

    def test_only_light_metadata_changes_and_no_encoding(self):
        with patch.object(music, 'to_wav', side_effect=lambda src, dst, *args: dst.touch()) as decode, self.fake_analysis(), patch.object(music, 'encode_pieces', side_effect=AssertionError('must never encode packs')):
            result = self.build()
        updated = yaml.safe_load((self.out / 'library.yml').read_text())
        self.assertEqual({k: v for k, v in self.lib.items() if k != 'lights'}, {k: v for k, v in updated.items() if k != 'lights'})
        self.assertTrue((self.out / 'library.yml').read_text().startswith('# Keep this comment'))
        self.assertEqual(result, updated['lights'])
        data = (self.out / 'lights-900-1.txt').read_bytes()
        self.assertEqual(hashlib.sha1(data).hexdigest(), result['sha1']); self.assertEqual(len(data), result['bytes'])
        self.assertIn('/music-8/lights-900-1.txt', result['url'])
        self.assertEqual({'song'}, set(lightmap.parse_map(data.decode())))
        self.assertEqual({'lights-900-1.txt', 'library.yml', 'REPORT.txt', 'LIGHT-REPORT.md'}, {p.name for p in self.out.iterdir()})
        self.assertEqual((False, -14, -2), decode.call_args.args[2:])
        self.assertEqual(self.text, self.library.read_text(), 'base metadata is never modified in place')
        report = (self.out / 'REPORT.txt').read_text()
        self.assertIn('Per-song light report', report)
        self.assertIn('| song | 2 | 7.5 | 1 / 1 |', report)
        self.assertIn('| song | 2 | 7.5 | 1 / 1 |', (self.out / 'LIGHT-REPORT.md').read_text())
        self.assertIn('music-8/lights-900-1.txt', (self.out / 'LIGHT-REPORT.md').read_text())

    def test_changed_source_is_rejected_before_decode(self):
        self.audio.write_bytes(b'changed source bytes')
        with patch.object(music, 'to_wav') as decode, self.assertRaisesRegex(lights.Fail, 'missing or changed'): self.build()
        decode.assert_not_called(); self.assertFalse(self.out.exists())

    def test_missing_source_is_rejected(self):
        self.audio.unlink()
        with self.assertRaisesRegex(lights.Fail, 'missing or changed'): self.build()

    def test_wrong_manifest_set_length_and_filename_rejected(self):
        cases = [dict(format=2), dict(normalize='false'), dict(lufs=float('nan')), dict(tp=100), dict(sets={}), dict(**{'java-sha1': 'wrong'}), dict(**{'bedrock-sha1': 'wrong'}),
                 dict(sets={'song': dict(self.meta['sets']['song'], seconds=17)}),
                 dict(sets={'song': dict(self.meta['sets']['song'], file='../song.wav')}),
                 dict(sets={'song': dict(self.meta['sets']['song'], sha256='bad')})]
        for change in cases:
            with self.subTest(change=change):
                self.manifest.write_text(json.dumps(dict(self.meta, **change)))
                with self.assertRaises(lights.Fail): self.build()
                self.assertFalse(self.out.exists())

    def test_analysis_failure_cannot_create_metadata(self):
        with patch.object(music, 'to_wav'), patch.object(music, 'analyse_lights', return_value=(None, ['no usable beat'])), self.assertRaisesRegex(lights.Fail, 'no usable light cues'): self.build()
        self.assertFalse(self.out.exists())

    def test_replaces_only_the_lights_block_or_adds_one(self):
        new = {'url': 'new', 'sha1': 'newhash', 'bytes': 50}
        for text in [self.text, self.text.replace('lights:\n  url: "https://github.com/WubcraftMC/wubmas-music/releases/download/music-8/lights.txt"\n  sha1: "cccccccccccccccccccccccccccccccccccccccc"\n  bytes: 100\n', '')]:
            changed = lights.replace_lights(text, new)
            expected = yaml.safe_load(text); expected['lights'] = new
            self.assertEqual(expected, yaml.safe_load(changed))
        with self.assertRaises(lights.Fail): lights.replace_lights('lights:\n  bytes: 1\nlights:\n  bytes: 2\n', new)

    def test_workflow_keeps_pack_assets_and_switches_metadata_last(self):
        workflow = Path(__file__).resolve().parents[1] / '.github/workflows/build-lights.yml'
        raw = workflow.read_text(); doc = yaml.load(raw, Loader=yaml.BaseLoader)
        self.assertIn('workflow_dispatch', doc['on'])
        self.assertEqual('build-music', doc['concurrency']['group'])
        commands = '\n'.join(step.get('run', '') for step in doc['jobs']['lights']['steps'])
        self.assertNotIn('make_music_packs.py', commands)
        self.assertNotIn('--version', commands)
        self.assertNotIn('release create', commands)
        self.assertNotIn('release delete', commands)
        uploads = [line for line in commands.splitlines() if 'gh release upload' in line]
        self.assertEqual(3, len(uploads)); self.assertIn('$LIGHT_ASSET', uploads[0]); self.assertIn('LIGHT-REPORT.md --clobber', uploads[1]); self.assertIn('library.yml --clobber', uploads[-1])
        self.assertLess(commands.index('CURRENT_TAG='), commands.index(uploads[0]))
        self.assertIn('--pattern light-sources.json', commands)

    def test_normal_publisher_includes_the_source_manifest(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/publish_release.py'
        spec = importlib.util.spec_from_file_location('publish_release_test', script)
        publisher = importlib.util.module_from_spec(spec); spec.loader.exec_module(publisher)
        build = self.root / 'normal-build'; build.mkdir()
        for name in ['WubmasMusic-java.zip', 'WubmasMusic-bedrock.mcpack', 'REPORT.txt', 'lights.txt', 'light-sources.json', 'LIGHT-REPORT.md']:
            (build / name).write_text('{}')
        (build / 'library.yml').write_text(self.text)
        (build / 'LIGHT-REPORT.md').write_text('## Per-song light report\n\n| song | 2 |\n')
        calls = []
        def gh(args, check=True):
            calls.append(args); return SimpleNamespace(returncode=0, stdout=b'[]')
        summary = self.root / 'summary.md'
        with patch.object(publisher, 'gh', gh), patch.dict(publisher.os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}):
            self.assertEqual(0, publisher.main(['WubcraftMC/wubmas-music', 'music-8', str(build)]))
        created = next(c for c in calls if c[:2] == ['release', 'create'])
        self.assertIn(str(build / 'light-sources.json'), created)
        self.assertIn(str(build / 'lights.txt'), created)
        self.assertIn(str(build / 'LIGHT-REPORT.md'), created)
        self.assertIn('## Per-song light report', summary.read_text())
        self.assertIn('| song | 2 |', summary.read_text())

    def test_old_lightmap_fails_before_audio_analysis(self):
        with patch.object(lights.lightmap, 'report_markdown', None), patch.object(music, 'to_wav') as decode:
            with self.assertRaisesRegex(lights.Fail, 'Update tool/lightmap.py'): self.build()
            decode.assert_not_called()


class RealAudioTests(unittest.TestCase):
    def test_full_build_manifest_then_lights_only_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); sets = root / 'sets'; sets.mkdir(); src = sets / 'song.wav'
            sr = 44100; out = np.zeros(sr * 16)
            tt = np.arange(int(sr * 0.2)) / sr
            kick = 0.7 * np.sin(2*np.pi*(55*tt + 85*0.03*(1-np.exp(-tt/0.03)))) * np.exp(-14*tt)
            for i in range(sr, len(out) - len(kick), sr // 2): out[i:i+len(kick)] += kick
            with wave.open(str(src), 'wb') as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
                w.writeframes((np.stack([out, out], axis=1)*32767).astype('<i2').tobytes())
            lineup = root / 'lineup.json'; lineup.write_text('[]')
            full = root / 'full'
            music.build(sets, full, kbps=48, bedrock_kbps=48, piece=2, normalize=False,
                        lineup_file=lineup, lights=True, version=108, quiet=True,
                        pack_url='https://github.com/WubcraftMC/wubmas-music/releases/download/music-8/WubmasMusic-java.zip',
                        bedrock_url='https://github.com/WubcraftMC/wubmas-music/releases/download/music-8/WubmasMusic-bedrock.mcpack',
                        lights_url='https://github.com/WubcraftMC/wubmas-music/releases/download/music-8/lights.txt')
            meta = json.loads((full / 'light-sources.json').read_text())
            self.assertEqual(lights.digest(src), meta['sets']['song']['sha256'])
            packs = {p.name: p.read_bytes() for p in full.iterdir() if p.suffix in ('.zip', '.mcpack')}
            rebuilt = root / 'lights'
            lights.build(sets, full / 'library.yml', full / 'light-sources.json', rebuilt,
                         'WubcraftMC/wubmas-music', 'music-8', 'lights-900-1.txt')
            self.assertEqual((full / 'lights.txt').read_bytes(), (rebuilt / 'lights-900-1.txt').read_bytes())
            for name, data in packs.items(): self.assertEqual(data, (full / name).read_bytes())
            before = yaml.safe_load((full / 'library.yml').read_text()); after = yaml.safe_load((rebuilt / 'library.yml').read_text())
            self.assertEqual(before['resource-pack'], after['resource-pack']); self.assertEqual(before['bedrock-pack'], after['bedrock-pack'])
            self.assertIn('mapped hits', (full / 'REPORT.txt').read_text())
            self.assertIn('Per-song light report', (full / 'LIGHT-REPORT.md').read_text())
            self.assertIn('Per-song light report', (rebuilt / 'REPORT.txt').read_text())
            # Reusing an output directory with lights disabled must not publish an old report.
            music.build(sets, full, kbps=48, bedrock_kbps=48, piece=2, normalize=False,
                        lineup_file=lineup, lights=False, version=108, quiet=True)
            self.assertFalse((full / 'LIGHT-REPORT.md').exists())


if __name__ == '__main__': unittest.main(verbosity=2)
