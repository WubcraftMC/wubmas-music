import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clean_music
import fetch_sources
import music_policy


class MusicPolicyTests(unittest.TestCase):
    def setUp(self):
        music_policy.artist_pattern.cache_clear()
        self.addCleanup(music_policy.artist_pattern.cache_clear)

    def test_artist_credits_and_alias_separators(self):
        for name in ['VOYDOOM - DEVIL MASTER.wav', 'NGHTOUT - Heavyweight.mp3',
                     'MOROS - RUTHLESS 2026.wav', 'infrno.flac', 'Yung Siete.wav',
                     'yungsiete.mp3', 'yung_siete final mix.wav',
                     'Ray Volpe - See You Drop (NGHTOUT FLIP).mp3',
                     'EXCISION X SULLIVAN - ENCORE MOROS FLIP.wav',
                     'VOYDOOM GOAT DUBZ - IMMOLATOR.mp3']:
            with self.subTest(name=name):
                self.assertTrue(music_policy.allowed_source(name))
        for name in ['ILLENIUM - Lonely.mp3', 'SLANDER - song.wav',
                     'notinfrno.mp3', 'voydoomish.wav', 'SARGE SET.mp3', 'song.mp3']:
            with self.subTest(name=name):
                self.assertFalse(music_policy.allowed_source(name))

    def test_invalid_policy_blocks_processing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'policy.json'
            for data in [{}, {'keep_artists': []}, {'keep_artists': ['']}, {'keep_artists': [42]}]:
                with self.subTest(data=data), patch.object(music_policy, 'POLICY', path):
                    music_policy.artist_pattern.cache_clear()
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        music_policy.allowed_source('infrno.wav')

    def test_cleanup_only_removes_unselected_source_audio(self):
        assets = [{'id':1, 'name':'ILLENIUM.mp3'}, {'id':2, 'name':'NGHTOUT.wav'},
                  {'id':3, 'name':'cover.png'}, {'id':4, 'name':'WubmasMusic-java.zip'},
                  {'id':7, 'name':'Freak.Full.song.wav'}]
        releases = [{'draft':True, 'name':'Wubmas music files', 'assets':assets},
                    {'draft':True, 'name':'Unrelated draft', 'assets':[{'id':5,'name':'other.wav'}]},
                    {'draft':False, 'assets':[{'id':6,'name':'SLANDER.mp3'}]}]
        with patch.object(clean_music, 'all_releases', return_value=releases), patch.object(clean_music, 'api') as api:
            clean_music.clean_sources('owner/repo')
        self.assertEqual([1,6], [call.args[1] for call in api.call_args_list])
        self.assertTrue(all(c.args[2] == 'DELETE' for c in api.call_args_list))

    def test_uncredited_pending_track_is_preserved_but_not_packaged(self):
        self.assertTrue(music_policy.pending_source('Freak.Full.song.wav'))
        self.assertFalse(music_policy.allowed_source('Freak.Full.song.wav'))

    def test_fetch_filters_before_limits_and_download(self):
        assets = [{'id':1, 'name':'ILLENIUM.mp3', 'size':10**10},
                  {'id':2, 'name':'infrno.wav', 'size':2}]
        releases = [{'draft':True, 'name':'Wubmas music files', 'assets':assets}]
        def download(args, to_file=None):
            self.assertTrue(args[-1].endswith('/2'))
            to_file.write(b'ok')
        with tempfile.TemporaryDirectory() as td, patch.object(fetch_sources, 'all_releases', return_value=releases), patch.object(fetch_sources, 'gh', side_effect=download) as gh:
            got = fetch_sources.fetch_draft('owner/repo', 'Wubmas music files', td,
                                           say=lambda *_:None, max_files=1, max_bytes=2)
            self.assertEqual(['infrno.wav'], [p.name for p in got])
            self.assertEqual(b'ok', got[0].read_bytes())
            self.assertEqual(1, gh.call_count)

    def test_repo_music_folder_uses_same_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); folder=root/'music'; dest=root/'sets'; folder.mkdir(); dest.mkdir()
            for name in ['SLANDER.mp3', 'Yung Siete.wav']:
                (folder/name).write_bytes(b'audio')
            got=fetch_sources.copy_folder(folder,dest,say=lambda *_:None)
            self.assertEqual(['Yung Siete.wav'], [p.name for p in got])
            self.assertFalse((dest/'SLANDER.mp3').exists())

    def test_old_pack_deletion_is_exact_and_future_assets_are_not_touched(self):
        old_id, name = next(iter(clean_music.OLD_PACKS.items()))
        def api(repo, asset_id, method='GET', missing_ok=False):
            if method == 'GET':
                return {'name':name} if asset_id == old_id else None
            return {}
        with patch.object(clean_music,'api',side_effect=api) as calls:
            clean_music.clean_old_packs('owner/repo')
        deletions=[c.args[1] for c in calls.call_args_list if len(c.args)>2 and c.args[2]=='DELETE']
        self.assertEqual([old_id],deletions)
        self.assertTrue(all(c.args[1] in clean_music.OLD_PACKS for c in calls.call_args_list))

    def test_old_asset_name_change_stops_deletion(self):
        with patch.object(clean_music,'api',return_value={'name':'source-code.zip'}) as api:
            with self.assertRaises(clean_music.Fail):
                clean_music.clean_old_packs('owner/repo')
        self.assertEqual(1,api.call_count)


if __name__ == '__main__':
    unittest.main()
