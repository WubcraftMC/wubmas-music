import copy
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml
import lightmap
import make_music_packs as music


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.playlist = self.root / 'playlist.json'
        self.lineup = [{'id': 'dj', 'name': 'DJ', 'start': '2026-12-19 14:00'},
                       {'id': 'song', 'test': True}, {'id': 'song_2', 'test': True}]

    def select(self, payload):
        self.playlist.write_text(json.dumps(payload))
        return music.select_test_tracks(self.lineup, self.playlist)

    def test_selection_retains_dj_slots_and_does_not_mutate_input(self):
        before = copy.deepcopy(self.lineup)
        chosen, excluded = self.select({'test_set_ids': ['song_2']})
        self.assertEqual(['dj', 'song_2'], [e['id'] for e in chosen])
        self.assertEqual(['song'], [e['id'] for e in excluded])
        self.assertEqual(before, self.lineup)
        self.assertEqual((self.lineup, []), music.select_test_tracks(self.lineup, None))
        self.assertEqual(([self.lineup[0]], self.lineup[1:]), self.select({'test_set_ids': []}))

    def test_invalid_selection_is_rejected(self):
        for payload in [[], {}, {'test_set_ids': 'song'}, {'test_set_ids': [1]},
                        {'test_set_ids': ['Song']}, {'test_set_ids': ['song', 'song']},
                        {'test_set_ids': ['missing']}, {'test_set_ids': ['dj']}]:
            with self.subTest(payload=payload), self.assertRaises(music.ToolError): self.select(payload)
        self.playlist.write_text('{broken')
        with self.assertRaisesRegex(music.ToolError, 'Cannot read test playlist'):
            music.select_test_tracks(self.lineup, self.playlist)
        self.playlist.unlink()
        with self.assertRaisesRegex(music.ToolError, 'Cannot read test playlist'):
            music.select_test_tracks(self.lineup, self.playlist)

    def test_nothing_selected_fails_before_decoding(self):
        sets = self.root / 'sets'; sets.mkdir(); (sets / 'song.wav').write_bytes(b'unchanged')
        lineup = self.root / 'lineup.json'; lineup.write_text('[]')
        self.select({'test_set_ids': []})
        with patch.object(music, 'to_wav') as decode, self.assertRaisesRegex(music.ToolError, 'No audio is selected'):
            music.build(sets, self.root / 'out', lineup_file=lineup, test_playlist=self.playlist)
        decode.assert_not_called(); self.assertFalse((self.root / 'out').exists())

    def test_cli_sample_all_and_demo(self):
        out = self.root / 'out'; out.mkdir(); (out / 'REPORT.txt').write_text('report')
        cases = [([], music.HERE.parent / 'test-playlist.json'),
                 (['--test-playlist', str(self.playlist)], str(self.playlist)),
                 (['--all-test-tracks'], None), (['--demo'], None)]
        for args, expected in cases:
            with self.subTest(args=args), patch.object(music, 'build') as build, patch.object(music, 'make_demo'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, music.main(['sets', '--out', str(out)] + args))
                self.assertEqual(expected, build.call_args.kwargs['test_playlist'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            music.main(['sets', '--test-playlist', str(self.playlist), '--all-test-tracks'])

    def test_workflow_passes_selection_and_keeps_audio_settings(self):
        workflow = music.HERE.parent / '.github/workflows/build-music.yml'
        doc = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
        inputs = doc['on']['workflow_dispatch']['inputs']; job = doc['jobs']['build']
        self.assertEqual(['Sample playlist', 'All songs'], inputs['test_tracks']['options'])
        self.assertEqual('Sample playlist', inputs['test_tracks']['default'])
        self.assertIn('test-playlist.json', doc['on']['push']['paths'])
        defaults = {'kbps': '64', 'piece': '2', 'long_piece': '120', 'bedrock_long_piece': '30', 'bedrock_kbps': '48'}
        for name, value in defaults.items(): self.assertEqual(value, inputs[name]['default'])
        for step in job['steps']:
            if 'run' in step: subprocess.run(['bash', '-n'], input=step['run'], text=True, check=True, capture_output=True)
        build_step = next(s['run'] for s in job['steps'] if s['name'] == 'Build the packs')
        stub = self.root / 'python3'; stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n'); stub.chmod(0o755)
        env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'], RUN_NUMBER='10', REPO='WubcraftMC/wubmas-music', TAG='music-10',
                   KBPS='64', PIECE='2', LONG_PIECE='120', BEDROCK_LONG_PIECE='30', BEDROCK_KBPS='48')
        for mode, flag in [('Sample playlist', '--test-playlist'), ('All songs', '--all-test-tracks')]:
            run = subprocess.run(['bash', '-e', '-c', build_step], env=dict(env, TEST_TRACKS=mode), text=True, capture_output=True, check=True)
            args = run.stdout.splitlines(); self.assertIn(flag, args)
            for flag_name, value in [('--kbps', '64'), ('--piece', '2'), ('--long-piece', '120'), ('--bedrock-long-piece', '30'), ('--bedrock-kbps', '48'), ('--version', '110')]:
                self.assertEqual(value, args[args.index(flag_name) + 1])
        run = subprocess.run(['bash', '-e', '-c', build_step], env=dict(env, TEST_TRACKS='invalid'), text=True, capture_output=True)
        self.assertNotEqual(0, run.returncode); self.assertNotIn('make_music_packs.py', run.stdout)


class RealPlaylistAudioTests(unittest.TestCase):
    def test_selected_audio_and_lights_only_and_all_tracks_restore(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); sets = root / 'sets'; sets.mkdir()
            sr = 44100; audio = np.zeros(sr * 6); t = np.arange(sr // 5) / sr
            kick = .7 * np.sin(2*np.pi*65*t) * np.exp(-14*t)
            for i in range(sr // 2, len(audio) - len(kick), sr // 2): audio[i:i+len(kick)] += kick
            for filename in ['DJ.wav', 'Foo-Bar.wav', 'Foo.Bar.wav']:
                with wave.open(str(sets / filename), 'wb') as w:
                    w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
                    w.writeframes((np.stack([audio, audio], axis=1)*32767).astype('<i2').tobytes())
            originals = {p.name: p.read_bytes() for p in sets.iterdir()}
            lineup = root / 'lineup.json'
            lineup.write_text(json.dumps([{'id': 'dj', 'name': 'DJ', 'start': '2026-12-19 14:00'},
                                          {'id': 'later', 'name': 'Later', 'start': '2026-12-19 15:00'}]))
            playlist = root / 'playlist.json'; playlist.write_text(json.dumps({'test_set_ids': ['foo_bar_2']}))
            kwargs = dict(kbps=64, bedrock_kbps=48, piece=2, long_piece=120, bedrock_long_piece=30,
                          normalize=False, lineup_file=lineup, lights=True, version=110, quiet=True,
                          pack_url='https://example.com/java.zip', bedrock_url='https://example.com/bedrock.mcpack', lights_url='https://example.com/lights.txt')
            for label, ids, selection in [('sample', {'dj', 'foo_bar_2'}, playlist), ('all', {'dj', 'foo_bar', 'foo_bar_2'}, None)]:
                out = root / label; result = music.build(sets, out, test_playlist=selection, **kwargs)
                self.assertEqual(ids, {e['id'] for e in result['sets']})
                self.assertEqual(ids | {'later'}, {e['id'] for e in result['lineup']})
                self.assertEqual(ids, set(json.loads((out / 'light-sources.json').read_text())['sets']))
                self.assertEqual(ids, set(lightmap.parse_map((out / 'lights.txt').read_text())))
                lib = yaml.safe_load((out / 'library.yml').read_text())
                self.assertEqual(ids | {'later'}, {e['id'] for e in lib['sets']})
                for name, path in [('WubmasMusic-java.zip', 'assets/minecraft/sounds.json'), ('WubmasMusic-bedrock.mcpack', 'sounds/sound_definitions.json')]:
                    with zipfile.ZipFile(out / name) as pack:
                        defs = pack.read(path).decode(); filenames = pack.namelist()
                        for sid in ids: self.assertIn(sid, defs)
                        if selection:
                            self.assertNotIn('foo_bar.', defs); self.assertNotIn('foo_bar/', '\n'.join(filenames))
                report = (out / 'REPORT.txt').read_text(); self.assertIn(f'{len(ids)-1} of 2 full test songs', report)
                if selection: self.assertIn('foo_bar: Foo-Bar.wav', report)
                for name, data in originals.items(): self.assertEqual(data, (sets / name).read_bytes())
            # No extra test songs is allowed when a scheduled DJ has source audio.
            playlist.write_text(json.dumps({'test_set_ids': []}))
            result = music.build(sets, root / 'dj-only', test_playlist=playlist, **dict(kwargs, lights=False, lights_url=''))
            self.assertEqual({'dj'}, {e['id'] for e in result['sets']})


if __name__ == '__main__': unittest.main(verbosity=2)
