import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import make_music_packs as music


class BedrockModesTests(unittest.TestCase):
    def test_modes_share_audio_and_cover_short_and_long_events(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); short=root/'short.ogg'; long=root/'long.ogg'
            short.write_bytes(b'short fixture'); long.write_bytes(b'long fixture')
            pack=root/'test.mcpack'
            sets=[{'id':'infrno','pieces':1,'files':[short],'files_blong':[long]}]
            music.bedrock_pack(pack,sets,'test','record',128)
            with zipfile.ZipFile(pack) as z:
                defs=json.loads(z.read('sounds/sound_definitions.json'))['sound_definitions']
                audio=[n for n in z.namelist() if n.endswith('.ogg')]
                self.assertEqual(2,len(audio))
                self.assertEqual(8,len(defs))
                for event in [music.ev('infrno',0),music.ev_blong('infrno',0)]:
                    base=defs[event]
                    self.assertEqual('record',base['category'])
                    self.assertTrue(base['sounds'][0]['stream'])
                    for mode, category, streamed in [('b','music',True),('c','ui',True),('d','record',False)]:
                        item=defs[music.mode_ev(mode,event)]
                        self.assertEqual(category,item['category'])
                        self.assertEqual(streamed,item['sounds'][0]['stream'])
                        self.assertEqual(base['sounds'][0]['name'],item['sounds'][0]['name'])


if __name__ == '__main__':
    unittest.main()
