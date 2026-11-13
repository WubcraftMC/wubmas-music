#!/usr/bin/env python3
"""Rebuild only lights for a published music build, without encoding or changing packs.
Needs ffmpeg and PyYAML. Source hashes/settings come from light-sources.json,
which a normal full build now publishes alongside its existing files.
"""
import argparse
import hashlib
import json
import math
import re
import tempfile
from pathlib import Path

import yaml
import lightmap
import make_music_packs as music


class Fail(Exception):
    pass


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def replace_lights(text, lights):
    """Preserve every other byte of the published YAML, including audio metadata."""
    block = ('lights:\n' + '\n'.join('  ' + key + ': ' + json.dumps(value)
             for key, value in lights.items()) + '\n')
    old = re.compile(r'^lights:[ \t]*\r?\n(?:[ \t]+[^\r\n]*\r?\n|\r?\n)*', re.M)
    found = list(old.finditer(text))
    if len(found) > 1: raise Fail('The published library contains duplicate lights sections.')
    if found:
        m = found[0]; return text[:m.start()] + block + text[m.end():]
    return text.rstrip('\n') + '\n' + block


def build(sets_dir, library, sources, out, repo, tag, asset):
    if not callable(getattr(lightmap, 'report_markdown', None)): raise Fail('Update tool/lightmap.py with the matching builder files before running this build.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo): raise Fail('Invalid repository name.')
    if not re.fullmatch(r'music-\d+', tag): raise Fail('The base release must be music-<number>.')
    if not re.fullmatch(r'lights-\d+-\d+\.txt', asset): raise Fail('Invalid unique light-map asset name.')
    text = Path(library).read_text(encoding='utf-8')
    lib = yaml.safe_load(text)
    manifest = json.loads(Path(sources).read_text(encoding='utf-8'))
    if not isinstance(lib, dict) or not isinstance(lib.get('sets'), list): raise Fail('Invalid published library.')
    if manifest.get('format') != 1 or not isinstance(manifest.get('sets'), dict): raise Fail('Invalid source manifest; run a full music build first.')
    if manifest.get('java-sha1') != lib.get('resource-pack', {}).get('sha1') or (lib.get('bedrock-pack') and manifest.get('bedrock-sha1') != lib['bedrock-pack'].get('sha1')):
        raise Fail('Source manifest belongs to different audio packs; run a full music build.')
    if not isinstance(manifest.get('normalize'), bool): raise Fail('Invalid normalization setting in source manifest.')
    for key in ('lufs', 'tp'):
        v = manifest.get(key)
        if not isinstance(v, (int, float)) or not math.isfinite(v) or not -70 <= v <= 0: raise Fail('Invalid audio level setting in source manifest.')
    active = {}
    for entry in lib['sets']:
        if not isinstance(entry, dict): raise Fail('Invalid set in the published library.')
        sid = entry.get('id')
        if not isinstance(sid, str) or not re.fullmatch(r'[a-z0-9_]{1,40}', sid) or sid in active: raise Fail('Invalid or duplicate set id.')
        if entry.get('pieces', 0) > 0: active[sid] = entry
    if not active or active.keys() != manifest['sets'].keys(): raise Fail('Source manifest does not match the published music sets; run a full music build.')
    verified = {}
    for sid, entry in active.items():
        source = manifest['sets'][sid]
        if not isinstance(source, dict): raise Fail('Invalid source manifest entry.')
        name = source.get('file', '')
        if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\\' in name: raise Fail('Invalid source filename.')
        seconds = entry.get('seconds')
        if not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or not 1 <= seconds <= 86400 or seconds != source.get('seconds'): raise Fail('Set lengths changed; run a full music build.')
        expected = source.get('sha256', '')
        path = Path(sets_dir) / name
        if not re.fullmatch(r'[a-f0-9]{64}', expected) or not path.is_file() or digest(path) != expected:
            raise Fail(f'{sid}: source music is missing or changed. Run a full music build; lights were not published.')
        verified[sid] = path
    results, warnings = {}, []
    with tempfile.TemporaryDirectory(prefix='wubmas_lights_') as tmp:
        for sid, src in verified.items():
            print(f'{sid}: analysing lights (audio packs unchanged)', flush=True)
            wav = Path(tmp) / (sid + '.wav')
            music.to_wav(src, wav, manifest['normalize'], manifest['lufs'], manifest['tp'])
            result, warn = music.analyse_lights(lightmap, wav, sid, active[sid]['seconds'])
            warnings.extend(warn)
            if result is None: raise Fail(f'{sid}: no usable light cues; keeping the published library unchanged. ' + '; '.join(warn))
            results[sid] = result
            wav.unlink()
    mapping = lightmap.format_map(results)
    if set(lightmap.parse_map(mapping)) != set(active): raise Fail('Light-map validation failed.')
    encoded = mapping.encode('utf-8')
    lights = {'url': f'https://github.com/{repo}/releases/download/{tag}/{asset}',
              'sha1': hashlib.sha1(encoded).hexdigest(), 'bytes': len(encoded)}
    updated = replace_lights(text, lights)
    parsed = yaml.safe_load(updated)
    if {k: v for k, v in parsed.items() if k != 'lights'} != {k: v for k, v in lib.items() if k != 'lights'}:
        raise Fail('A lights-only build must not change audio metadata.')
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    (out / asset).write_bytes(encoded)
    (out / 'library.yml').write_text(updated, encoding='utf-8')
    report = (f'## Wubmas lights updated for {tag}\n\n{len(results)} sets analysed.\n\n'
              'Java and Bedrock pack URLs, hashes, sizes and versions are unchanged.\n'
              'Install WubmasMusic 1.14.2 before running `/wubmusic update` on the server.\n'
              'No pack download or Geyser restart is needed for this lights-only update.\n')
    song_report = lightmap.report_markdown(results) + f'\nReport for `{tag}/{asset}`.\n'
    report += '\n' + song_report
    if warnings: report += '\nWarnings:\n' + '\n'.join('- ' + w for w in warnings) + '\n'
    (out / 'REPORT.txt').write_text(report, encoding='utf-8')
    (out / 'LIGHT-REPORT.md').write_text(song_report, encoding='utf-8')
    return lights


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('sets'); ap.add_argument('--library', required=True); ap.add_argument('--sources', required=True)
    ap.add_argument('--out', default='lights-build'); ap.add_argument('--repo', required=True)
    ap.add_argument('--tag', required=True); ap.add_argument('--asset', required=True)
    a = ap.parse_args(argv)
    try: build(a.sets, a.library, a.sources, a.out, a.repo, a.tag, a.asset)
    except (Fail, music.ToolError, OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as e:
        print('ERROR: ' + str(e)); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
