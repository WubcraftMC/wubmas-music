#!/usr/bin/env python3
"""Wubmas music pack tool: turns the DJ set files in a folder into the two resource packs and the plugin config.

  python make_music_packs.py SETS_FOLDER [--out build] [--kbps 96]

SETS_FOLDER holds the audio files, with any names. A file whose name is a DJ's id or name from lineup.json, or starts or ends with it ("DxBuddha - final mix.mp3"),
is that DJ's set and starts on the DJ's time. Every other file becomes a test set ("Test: <file name>") that never starts by itself: /wubmusic start <id> 0.
It writes into --out:
  WubmasMusic-java.zip       the Java resource pack (the server hands it to Java players)
  WubmasMusic-bedrock.mcpack the Bedrock pack (goes in Geyser's packs folder)
  WubmasMusic-bedrock-vN.zip with --bedrock-remote: the same Bedrock pack for a web link (Geyser's resource-pack-urls), so the server does not send it to the players
  lights.txt                 when the stage lights follow the music: where every beat, bar, break and drop of every set is (found by lightmap.py, next to this script)
  server-files/              config.yml, lights.txt and the Bedrock pack (not with --bedrock-remote) in the server's own folder layout: drag its plugins folder onto the server
  config.yml                 the WubmasMusic plugin config with the sets, piece counts and lengths filled in
  REPORT.txt                 sizes, loudness, warnings, and the server.properties lines

What it does to each set: levels it to the same loudness (so no DJ is louder than another), cuts it into 15 second pieces that overlap
by 0.3 seconds with a crossfade (a sound cannot be started in the middle, so the plugin starts one piece after another on the clock),
and encodes each piece as Ogg Vorbis. It needs Python 3.8 or newer and ffmpeg (https://ffmpeg.org); nothing else.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, uuid, wave, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".aif", ".aiff", ".opus", ".wma", ".mp4", ".mkv", ".webm"}
SR = 44100
ID = re.compile(r"[a-z0-9_]+")
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


class ToolError(Exception):
    pass


def say(msg=""):
    print(msg, flush=True)


# ------------------------------------------------------------------ ffmpeg
def ffmpeg_path():
    p = shutil.which("ffmpeg")
    if not p:
        raise ToolError("ffmpeg was not found. Install it (Windows: winget install Gyan.FFmpeg, then open a new terminal; Mac: brew install ffmpeg) and run this again.")
    return p


def run_ffmpeg(args, data=None):
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error" if "loudnorm" not in " ".join(args) else "info"] + args
    r = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")


def last_json(text):
    i = text.rfind("{")
    j = text.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        return json.loads(text[i:j + 1])
    except ValueError:
        return None


def to_wav(src, dst, normalize, lufs, tp):
    """Converts any audio file to 44.1 kHz stereo 16-bit WAV; with normalize, first measures the file and then levels it to `lufs` (a linear gain, so the DJ's dynamics are kept)."""
    stats = {"normalized": False}
    af = []
    if normalize:
        rc, _, err = run_ffmpeg(["-i", str(src), "-vn", "-af", f"loudnorm=I={lufs}:TP={tp}:LRA=11:print_format=json", "-f", "null", "-"])
        m = last_json(err)
        if rc != 0 or not m:
            raise ToolError(f"ffmpeg could not read {Path(src).name}:\n{err.strip()[-600:]}")
        try:
            mi = float(m["input_i"]); mtp = float(m["input_tp"]); mlra = float(m["input_lra"]); mth = float(m["input_thresh"]); off = float(m["target_offset"])
        except (KeyError, ValueError):
            raise ToolError(f"{Path(src).name} is silent or too quiet to measure")
        if mi < -60:
            raise ToolError(f"{Path(src).name} is silent or too quiet to measure ({mi} LUFS)")
        stats.update(input_i=mi, input_tp=mtp, normalized=True)
        af = ["-af", f"loudnorm=I={lufs}:TP={tp}:LRA=11:measured_I={mi}:measured_TP={mtp}:measured_LRA={mlra}:measured_thresh={mth}:offset={off}:linear=true:print_format=json"]
    rc, _, err = run_ffmpeg(["-y", "-i", str(src), "-vn"] + af + ["-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le", str(dst)])
    if rc != 0:
        raise ToolError(f"ffmpeg could not read {Path(src).name}:\n{err.strip()[-600:]}")
    if normalize:
        m = last_json(err)
        if m:
            try: stats.update(output_i=float(m["output_i"]), output_tp=float(m["output_tp"]))
            except (KeyError, ValueError): pass
    return stats


# ------------------------------------------------------------------ the light map
def load_lightmap():
    """The beat analyzer (lightmap.py, next to this script); None when it is not there."""
    try:
        if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))
        import lightmap
        return lightmap
    except ImportError:
        return None


def analyse_lights(lm, wav, sid, seconds):
    """(the analysis of one set, [warnings]); the analysis is None when there are no lights for it. Whatever goes wrong in here is a warning and never stops the music from being built."""
    try:
        r = lm.analyze(wav, run_ffmpeg)
    except lm.AnalysisError as e:
        why = str(e).strip().splitlines()[0].rstrip(": ") if str(e).strip() else "the beat analysis failed"
        return None, [f"{sid}: no lights for this set ({why}). The music is not affected."]
    except Exception as e:   # noqa: a bug in the analysis must never stop the music from being built
        return None, [f"{sid}: no lights for this set (the beat analysis failed: {type(e).__name__}: {e}). The music is not affected."]
    warn = [f"{sid}: {w}" for w in r["warnings"]]
    limit = int(seconds * 1000)
    tokens = [(t, c) for t, c in r["tokens"] if t <= limit]       # (the light map must not be longer than the music: the plugin compares the two lengths)
    if not tokens:
        return None, [f"{sid}: no lights for this set" + (" (" + "; ".join(r["warnings"]) + ")" if r["warnings"] else " (no beat was found)") + ". The music is not affected."]
    return dict(r, tokens=tokens, seconds=seconds), warn


def measure(src):
    """Integrated loudness and true peak of a file (for the report)."""
    rc, _, err = run_ffmpeg(["-i", str(src), "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    m = last_json(err)
    try: return float(m["input_i"]), float(m["input_tp"])
    except Exception: return None, None


# ------------------------------------------------------------------ cutting
def plan(duration, piece, tail_min):
    """How many pieces, and how long the set is. A tail shorter than tail_min seconds is dropped (it would be a piece of a second or two)."""
    n = int(duration // piece)
    tail = duration - n * piece
    if tail >= tail_min or n == 0:
        return n + 1, round(duration, 3)
    return n, round(n * piece, 3)


BASS_HZ = 400          # below this pitch the pieces are joined by a short hard switch instead of a crossfade (see join_graph)
SWITCH_S = 0.005       # how long that switch takes
PRE_S = 0.5            # audio read before and after a piece so that the bass filter has settled where the piece begins and ends


def join_graph(i, count, dur, half, hz, end_fade, pre_frames, n_frames):
    """The ffmpeg filter graph that makes piece i of `count` when the joins are bass-switched.
    Two sounds that the game starts a few milliseconds apart and that overlap in a crossfade add up with a time shift, and the bass of the two cancels for a moment (the "dip", "the low end cut out").
    So the sound is split at `hz`: the part above it crossfades over the whole overlap as always, the part below it (the bass) is hard-switched from one piece to the next in SWITCH_S in the middle of the overlap, so that two copies of the bass are hardly ever heard together.
    The part below is a zero-phase low pass (a 2nd order low pass run forwards and then backwards, so it delays nothing) and the part above is the original minus the same low pass, so when the pieces start exactly on time everything adds up to the original again.
    (A filter that delays the sound leaves part of the bass in the part above, and that part is crossfaded again.)
    The audio starts pre_frames before the piece and goes on after it so that the filter has settled; n_frames of it are kept."""
    ramp, s = 2 * half, SWITCH_S
    trim = f"atrim=start_sample={pre_frames}:end_sample={pre_frames + n_frames},asetpts=PTS-STARTPTS"
    hi, lo, post = [trim], [trim], []
    if i > 0:
        hi.append(f"afade=t=in:st=0:d={ramp:.4f}:curve=tri")
        lo.append(f"afade=t=in:st={half - s / 2:.4f}:d={s:.4f}:curve=tri")
    if i < count - 1:
        hi.append(f"afade=t=out:st={dur - ramp:.4f}:d={ramp:.4f}:curve=tri")
        lo.append(f"afade=t=out:st={dur - half - s / 2:.4f}:d={s:.4f}:curve=tri")
    if i == count - 1 and end_fade > 0: post.append(f"afade=t=out:st={max(0.0, dur - end_fade):.4f}:d={min(end_fade, dur):.4f}")
    return (f"[0:a]asplit=2[x][y];[x]lowpass=f={hz:g}:poles=2,areverse,lowpass=f={hz:g}:poles=2,areverse,asplit=2[l1][l2];"
            f"[y][l1]amerge=inputs=2,pan=stereo|c0=c0-c2|c1=c1-c3,{','.join(hi)}[h];"
            f"[l2]{','.join(lo)}[b];"
            f"[b][h]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3{''.join(',' + f for f in post)}[o]")


def encode_pieces(wav_path, out_dir, set_id, piece, half, count, kbps, end_fade, channel=None, prefix="p", end_s=None, bass_hz=0):
    """Writes p0000.ogg ... for one set (prefix="long": long0000.ogg ..., the long pieces; end_s: where the audio of the set ends, in seconds, so that the last long piece ends where the last short one does). Piece i covers [i*piece - half, (i+1)*piece + half] and fades in and out over 2*half so that, started 'half' early by the plugin, the overlaps add up to the original.
    bass_hz > 0 (stereo pieces only): the pieces are joined with a bass switch, see join_graph.
    channel=None writes the stereo pieces; "l" or "r" writes the left or the right channel alone as a mono piece (p0000_l.ogg, p0000_r.ogg), for the Java pack's split layout."""
    if channel not in (None, "l", "r"): raise ValueError("channel is None, 'l' or 'r'")
    files = []
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 2 and w.getsampwidth() == 2
        total = w.getnframes()
        if end_s is not None: total = min(total, round(end_s * SR))
        ramp = 2 * half
        for i in range(count):
            a = max(0, round((i * piece - half) * SR))
            b = min(total, round(((i + 1) * piece + half) * SR))
            switch = bass_hz > 0 and not channel and ramp > 0
            pre = min(a, round(PRE_S * SR)) if switch else 0
            post = min(total - b, round(PRE_S * SR)) if switch else 0
            w.setpos(a - pre)
            raw = w.readframes(b - a + pre + post)
            dur = (b - a) / SR
            filters = []
            if i > 0 and ramp > 0: filters.append(f"afade=t=in:st=0:d={ramp:.4f}:curve=tri")
            if i < count - 1 and ramp > 0: filters.append(f"afade=t=out:st={dur - ramp:.4f}:d={ramp:.4f}:curve=tri")
            if i == count - 1 and end_fade > 0: filters.append(f"afade=t=out:st={max(0.0, dur - end_fade):.4f}:d={min(end_fade, dur):.4f}")
            if channel: filters.insert(0, "pan=mono|c0=c0" if channel == "l" else "pan=mono|c0=c1")
            args = ["-y", "-f", "s16le", "-ar", str(SR), "-ac", "2", "-i", "-"]
            if switch: args += ["-filter_complex", join_graph(i, count, dur, half, bass_hz, end_fade, pre, b - a), "-map", "[o]"]
            elif filters: args += ["-af", ",".join(filters)]
            f = Path(out_dir) / set_id / (f"{prefix}{i:04d}.ogg" if not channel else f"{prefix}{i:04d}_{channel}.ogg")
            f.parent.mkdir(parents=True, exist_ok=True)
            args += ["-c:a", "libvorbis", "-b:a", f"{kbps}k", "-ar", str(SR), "-ac", "1" if channel else "2", "-fflags", "+bitexact", "-flags:a", "+bitexact", str(f)]
            rc, _, err = run_ffmpeg(args, data=raw)
            if rc != 0 or not f.exists():
                raise ToolError(f"ffmpeg could not encode {'long ' if prefix != 'p' else ''}piece {i + 1} of {set_id}:\n{err.strip()[-600:]}")
            files.append(f)
    return files


# ------------------------------------------------------------------ packs
def ev(set_id, i): return f"wubmas.{set_id}.p{i:04d}"
def ev_long(set_id, k): return f"wubmas.{set_id}.long{k:04d}"
def ev_blong(set_id, k): return f"wubmas.{set_id}.blong{k:04d}"       # a Bedrock long piece (its length is bedrock-long-piece-seconds, not necessarily the Java one)
def long_count(count, ratio): return -(-count // ratio)       # how many long pieces hold `count` short ones (the last one may be shorter)


LAYOUTS = ("split", "stereo")
MONO_SHARE = 0.625      # a mono channel is encoded at this share of the stereo bitrate: the two channels together are a little more than the stereo piece (64 kbps stereo -> 40 + 40)


def mono_kbps(kbps): return max(32, round(kbps * MONO_SHARE))


def write_zip(path, entries):
    """entries: list of (name, bytes or Path). Fixed order and timestamps, stored (Ogg is already compressed), so the same audio gives the same file."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, data in entries:
            zi = zipfile.ZipInfo(name, ZIP_TIME); zi.compress_type = zipfile.ZIP_STORED; zi.external_attr = 0o644 << 16
            z.writestr(zi, data.read_bytes() if isinstance(data, Path) else data)


STAGE_DIR = HERE / "stage_assets"   # the DJ models and textures (CDJs, mixer): the Java pack carries them next to the music


def stage_entries(stage_dir):
    """(name, Path) for every file under stage_dir, in a fixed order. Names are paths inside the pack and must start with assets/."""
    if not stage_dir or not Path(stage_dir).is_dir(): return []
    root, out = Path(stage_dir), []
    for f in sorted(root.rglob("*"), key=lambda q: q.relative_to(root).as_posix()):
        rel = f.relative_to(root).as_posix()
        if not f.is_file() or any(part.startswith(".") for part in rel.split("/")): continue
        if not rel.startswith("assets/"): raise ToolError(f"{f}: files in the stage_assets folder must be inside an 'assets' folder, as in a resource pack (assets/wubmas/models/...).")
        if rel == "assets/minecraft/sounds.json": raise ToolError("stage_assets must not have its own assets/minecraft/sounds.json: the builder writes that one.")
        out.append((rel, f))
    return out


def java_pack(path, sets, desc, stage=(), layout="stereo"):
    """layout "stereo": one stereo sound per piece (Minecraft plays a stereo sound at the player's ears: the same volume everywhere). "split": the left and the right channel as two mono sounds per piece
    (wubmas.<set>.pNNNN.l and .r, from p<NNNN>_l.ogg and _r.ogg), which the plugin plays from a left and a right speaker at the stage so that the game fades them with the distance.
    A set with "files_long" (stereo only) also has its long pieces, wubmas.<set>.longNNNN; its short pieces for Java are then "files_java" (a lower bitrate, they are only heard by players who joined a moment ago) if it has them."""
    if layout not in LAYOUTS: raise ToolError(f"the Java layout is split or stereo (got {layout!r})")
    sounds, entries = {}, []
    for s in sets:
        if s.get("files_long") and layout != "stereo": raise ToolError("long pieces need the stereo Java layout")
        for k, f in enumerate(s.get("files_long") or []):
            sounds[ev_long(s["id"], k)] = {"sounds": [{"name": f"wubmas/{s['id']}/long{k:04d}", "stream": True}]}
            entries.append((f"assets/minecraft/sounds/wubmas/{s['id']}/long{k:04d}.ogg", f))
        for i in range(s["pieces"]):
            if layout == "split":
                for ch, key in (("l", "files_l"), ("r", "files_r")):
                    sounds[f"{ev(s['id'], i)}.{ch}"] = {"sounds": [{"name": f"wubmas/{s['id']}/p{i:04d}_{ch}", "stream": True}]}
                    entries.append((f"assets/minecraft/sounds/wubmas/{s['id']}/p{i:04d}_{ch}.ogg", s[key][i]))
                continue
            sounds[ev(s["id"], i)] = {"sounds": [{"name": f"wubmas/{s['id']}/p{i:04d}", "stream": True}]}
            entries.append((f"assets/minecraft/sounds/wubmas/{s['id']}/p{i:04d}.ogg", (s.get("files_java") or s["files"])[i]))
    # min_format / max_format cover every client since 1.21.9 (the final range for 26.x was not published when this was made); a client outside it would still load the pack
    mcmeta = {"pack": {"min_format": 97, "max_format": 200, "description": desc}}
    head = [("pack.mcmeta", (json.dumps(mcmeta, indent=2) + "\n").encode()), ("assets/minecraft/sounds.json", (json.dumps(sounds, indent=1) + "\n").encode())]
    write_zip(path, head + list(stage) + entries)
    return sounds


def bedrock_pack(path, sets, desc, category, version, key="wubmas-music", title="Wubmas Music"):
    defs, entries = {}, []
    for s in sets:
        for i in range(s["pieces"]):
            defs[ev(s["id"], i)] = {"category": category, "sounds": [{"name": f"sounds/wubmas/{s['id']}/p{i:04d}", "stream": True, "volume": 1.0, "is3D": False}]}
            entries.append((f"sounds/wubmas/{s['id']}/p{i:04d}.ogg", s["files"][i]))
        for k, f in enumerate(s.get("files_blong") or []):
            defs[ev_blong(s["id"], k)] = {"category": category, "sounds": [{"name": f"sounds/wubmas/{s['id']}/blong{k:04d}", "stream": True, "volume": 1.0, "is3D": False}]}
            entries.append((f"sounds/wubmas/{s['id']}/blong{k:04d}.ogg", f))
    manifest = {"format_version": 2,
                "header": {"name": title, "description": desc, "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, key + ".header.wubcraft")), "version": [1, 0, version], "min_engine_version": [1, 20, 0]},
                "modules": [{"type": "resources", "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, key + ".resources.wubcraft")), "version": [1, 0, version], "description": "sounds"}]}
    head = [("manifest.json", (json.dumps(manifest, indent=2) + "\n").encode()),
            ("sounds/sound_definitions.json", (json.dumps({"format_version": "1.14.0", "sound_definitions": defs}, indent=1) + "\n").encode())]
    write_zip(path, head + entries)
    return defs


BEDROCK_REMOTE_DIR = "WubmasMusic"
LONG_PATH = 80      # Bedrock (some consoles) has trouble with a file path of this many characters or more; Geyser warns about it


def bedrock_remote_zip(src, dst):
    """The Bedrock pack again, for Geyser's `resource-pack-urls`: every file inside a folder (Geyser: a pack with its manifest.json at the root of the zip may not work from a web link, and the client then falls back to the server)."""
    with zipfile.ZipFile(src) as z: entries = [(f"{BEDROCK_REMOTE_DIR}/{i.filename}", z.read(i.filename)) for i in z.infolist()]
    write_zip(dst, entries)
    return max(len(n) for n, _ in entries)


def bedrock_remote_note(name, size, longest, pack_name):
    lines = ["", "Bedrock pack from a web link (so the server does not send it to every phone and console):",
             f"  {name} ({size / 1e6:.1f} MB) is the Bedrock pack again, for a link. Host it somewhere whose link ends in the file and answers with the type application/zip and the exact length",
             "  (raw.githubusercontent.com answers text/plain, so it does not work; Geyser says so in the console when the link is wrong). Then, in plugins/Geyser-Spigot/config.yml, under advanced:",
             "      resource-pack-urls:", f"        - \"https://.../{name}\"",
             f"  and remove {pack_name} from plugins/Geyser-Spigot/packs (the same pack twice does not work). Restart: Geyser downloads the pack once at start-up, and the players get it from the link.",
             "  config.yml has library.bedrock: false, so /wubmusic update does not copy a Bedrock pack into Geyser's packs folder either (that would be the same pack twice).",
             f"  The longest path in the pack is {longest} characters ({LONG_PATH} or more would cause trouble on some consoles)." if longest < LONG_PATH else
             f"  WARNING: the longest path in the pack is {longest} characters; {LONG_PATH} or more causes trouble on some Bedrock platforms. Use shorter set ids."]
    return "\n".join(lines) + "\n"


def yaml_str(s): return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_key(text, key, value):
    """Changes the value of a top-level or indented `key: value` line of the template, keeping the comment after it."""
    new, n = re.subn(rf"^(\s*{re.escape(key)}:\s*)[^\s#]+", lambda m: m.group(1) + value, text, count=1, flags=re.M)
    if n != 1: raise ToolError(f"config-template.yml has no '{key}:' line")
    return new


def write_config(path, template, lineup, built, source, piece=15.0, overlap=0.3, pack_url="", pack_sha1="", layout="stereo", long_piece=0.0, bedrock_long_piece=0.0, bedrock_remote=False):
    text = template.read_text(encoding="utf-8")
    cut = re.search(r"^sets:\s*$", text, re.M)
    if not cut: raise ToolError("config-template.yml has no 'sets:' line")
    head = text[:cut.start()]
    head = set_key(head, "piece-seconds", f"{piece:g}"); head = set_key(head, "overlap-seconds", f"{overlap:g}"); head = set_key(head, "source", source)
    head = set_key(head, "url", yaml_str(pack_url)); head = set_key(head, "sha1", yaml_str(pack_sha1)); head = set_key(head, "layout", layout); head = set_key(head, "long-piece-seconds", f"{long_piece:g}"); head = set_key(head, "bedrock-long-piece-seconds", f"{bedrock_long_piece:g}")
    if bedrock_remote:      # the Bedrock pack comes from a web link: /wubmusic update must not also copy it into Geyser's packs folder (the same pack twice does not work)
        head = set_key(head, "bedrock", "false")
        head, n = re.subn(r"^(\s*bedrock:\s*false)[ ]*#.*$", r"\1                   # false: the Bedrock pack comes from a web link (Geyser's resource-pack-urls), so /wubmusic update must not copy one into Geyser's packs folder", head, count=1, flags=re.M)
        if n != 1: raise ToolError("config-template.yml: the 'bedrock:' line has no comment to replace")
    lines = ["sets:"]
    for e in lineup:
        b = built.get(e["id"])
        extra = f", pieces: {b['pieces']}, seconds: {b['seconds']}" if b else ", pieces: 0"
        lines.append(f"  - {{id: {e['id']}, name: {yaml_str(e['name'])}, start: {yaml_str(e['start'])}{extra}}}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def write_library(path, lineup, built, piece, overlap, pack_url, pack_sha1, bedrock_url="", bedrock_sha1="", bedrock_bytes=0, build=1, built_at=None, lights_url="", lights_sha1="", lights_bytes=0, layout="stereo", long_piece=0.0, bedrock_long_piece=0.0):
    """library.yml: everything /wubmusic update needs to switch the server to this build (the Java pack's address and SHA-1, the Bedrock pack, the sets with their lengths and start times)."""
    from datetime import datetime, timezone
    when = built_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = ["# Written by the Wubmas music builder. /wubmusic update on the server downloads this file and uses it instead of the sets and the resource-pack in config.yml.",
         "# Do not edit it: the next update replaces it. (Delete it and run /wubmusic reload to go back to config.yml.)",
         f"build: {int(build)}", f"built: {yaml_str(when)}", f"piece-seconds: {piece:g}", f"overlap-seconds: {overlap:g}"]
    if long_piece: L.append(f"long-piece-seconds: {long_piece:g}")
    if bedrock_long_piece: L.append(f"bedrock-long-piece-seconds: {bedrock_long_piece:g}")
    L += ["resource-pack:", f"  url: {yaml_str(pack_url)}", f"  sha1: {yaml_str(pack_sha1)}", f"  layout: {layout}"]
    if bedrock_url:
        L += ["bedrock-pack:", f"  url: {yaml_str(bedrock_url)}", f"  sha1: {yaml_str(bedrock_sha1)}", f"  bytes: {int(bedrock_bytes)}"]
    if lights_url:
        L += ["lights:", f"  url: {yaml_str(lights_url)}", f"  sha1: {yaml_str(lights_sha1)}", f"  bytes: {int(lights_bytes)}"]
    L.append("sets:")
    for e in lineup:
        b = built.get(e["id"])
        extra = f", pieces: {b['pieces']}, seconds: {b['seconds']}" if b else ", pieces: 0"
        L.append(f"  - {{id: {e['id']}, name: {yaml_str(e['name'])}, start: {yaml_str(e['start'])}{extra}}}")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
    return h.hexdigest()


# ------------------------------------------------------------------ main
def set_id_for(p):
    sid = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]+", "_", p.stem.lower().strip())).strip("_")
    return sid


TEST_START = "2099-01-01 00:00"   # the start time of a file that is not a DJ's set: far away, so it never starts by itself (start it by hand: /wubmusic start <id> 0)


def name_key(text):
    """Letters and digits only, lower case: 'Yung Siete', 'yung_siete' and 'YUNG.SIETE' are all 'yungsiete'."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def match_files(files, lineup):
    """Decides which file is which DJ's set. A file is a DJ's when its name is the DJ's id or name (any capitals, spaces, dots or dashes), or starts or ends with it ('DxBuddha - Wubmas mix FINAL.mp3', 'Wubmas set - Gnarlie.wav').
    A name in the middle does not count: 'Artist - Song (INFRNO Remix)' is a song that credits INFRNO, not INFRNO's set.
    Anything else is a test set. A file that could be two DJs', or a DJ with two competing files, is never given a time slot: it becomes a test set and the report says so.
    Returns (assigned {lineup id: (file, how)}, tests [file], warnings [text])."""
    keys = {e["id"]: {k for k in (name_key(e["id"]), name_key(e["name"])) if k} for e in lineup}
    exact, part, warnings, loose = {}, {}, [], []
    for p in files:
        k = name_key(p.stem)
        ex = [i for i, ks in keys.items() if k in ks]
        if ex: exact.setdefault(ex[0], []).append(p); continue
        pt = [i for i, ks in keys.items() if any(len(x) >= 5 and (k.startswith(x) or k.endswith(x)) for x in ks)]
        if len(pt) == 1: part.setdefault(pt[0], []).append(p)
        elif len(pt) > 1:
            warnings.append(f"{p.name} could be any of {', '.join(pt)}, so it is a test set and not on anyone's time slot. Rename it after one DJ.")
            loose.append(p)
        else: loose.append(p)
    assigned, tests = {}, list(loose)
    for i in keys:
        e, pt = exact.get(i, []), part.get(i, [])
        if len(e) == 1:
            assigned[i] = (e[0], "by its name")
            for p in pt: tests.append(p); warnings.append(f"{p.name} also looks like {i}'s, but {e[0].name} is the file used for {i}. {p.name} is a test set.")
        elif len(e) > 1:
            tests += e + pt; warnings.append(f"{i} has more than one file ({', '.join(p.name for p in e + pt)}). None of them has the {i} time slot, so {i} has no music. Delete the extra ones in the draft release.")
        elif len(pt) == 1: assigned[i] = (pt[0], "its name starts or ends the file name")
        elif len(pt) > 1:
            tests += pt; warnings.append(f"{i} has more than one file ({', '.join(p.name for p in pt)}). None of them has the {i} time slot, so {i} has no music. Delete the extra ones in the draft release.")
    import difflib
    names = {name_key(e["name"]): e["id"] for e in lineup}; names.update({name_key(e["id"]): e["id"] for e in lineup})
    for p in sorted(loose, key=lambda q: q.name):
        close = difflib.get_close_matches(name_key(p.stem), list(names), n=1, cutoff=0.75)
        if close and names[close[0]] not in assigned: warnings.append(f"{p.name} looks like a spelling of {names[close[0]]} but does not match, so it is a test set and {names[close[0]]} has no music. Rename the file if it is {names[close[0]]}'s.")
    return assigned, sorted(tests, key=lambda q: q.name), warnings


def test_set_for(p, taken):
    """A made-up set for a file that is not a DJ's: an id from the file name (short, lower case, never one that is taken) and the name 'Test: ...'."""
    base = set_id_for(p)[:30].strip("_") or "track"
    sid, n = base, 2
    while sid in taken: sid = f"{base}_{n}"; n += 1
    pretty = re.sub(r"[\s._]+", " ", p.stem).strip()[:40].strip()
    return {"id": sid, "name": f"Test: {pretty}" if pretty else f"Test: {sid}", "start": TEST_START, "test": True}


def select_test_tracks(lineup, playlist_file):
    """Filter only test sets, after every source has its stable id. Never filter DJ slots."""
    if playlist_file is None: return list(lineup), []
    try: payload = json.loads(Path(playlist_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e: raise ToolError(f"Cannot read test playlist {playlist_file}: {e}. Restore test-playlist.json or choose All songs.")
    selected = payload.get("test_set_ids") if isinstance(payload, dict) else None
    if not isinstance(selected, list) or any(not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", sid) for sid in selected):
        raise ToolError("The test playlist must be an object with test_set_ids: a list of test ids from the build report.")
    if len(selected) != len(set(selected)): raise ToolError("The test playlist contains duplicate ids.")
    tests = {e["id"] for e in lineup if e.get("test")}
    missing = [sid for sid in selected if sid not in tests]
    if missing: raise ToolError("These playlist ids are not available test songs: " + ", ".join(missing) + ". Check the source names or edit test-playlist.json. Scheduled DJ sets are always included separately.")
    wanted = set(selected)
    excluded = [e for e in lineup if e.get("test") and e["id"] not in wanted]
    return [e for e in lineup if not e.get("test") or e["id"] in wanted], excluded


def build(sets_dir, out, kbps=96, piece=15.0, overlap=0.3, lufs=-14.0, tp=-2.0, normalize=True, lineup_file=None, source="record", cap_mb=250, tail_min=2.0, end_fade=1.0, extra_lineup=None, quiet=False, demo=False, pack_url="", bedrock_url="", version=None, strict_ids=False, short_min=10.0, stage_dir=None, lights=False, lights_url="", java_layout="stereo", long_piece=0.0, catchup_kbps=None, bass_hz=BASS_HZ, bedrock_long_piece=0.0, bedrock_catchup_kbps=None, bedrock_kbps=None, bedrock_remote=False, test_playlist=None):
    sets_dir = Path(sets_dir); out = Path(out)
    if java_layout not in LAYOUTS: raise ToolError(f"the Java layout is split or stereo (got {java_layout!r})")
    if pack_url and not re.fullmatch(r"https?://[\x21-\x7e]{3,2000}", pack_url): raise ToolError(f"--pack-url must be a web address that starts with https:// and has no spaces or accents (got {pack_url!r}).")
    if bedrock_url and not re.fullmatch(r"https?://[\x21-\x7e]{3,2000}", bedrock_url): raise ToolError(f"--bedrock-url must be a web address that starts with https:// and has no spaces or accents (got {bedrock_url!r}).")
    if bedrock_url and not pack_url: raise ToolError("--bedrock-url needs --pack-url as well (library.yml has the Java pack first).")
    if lights_url and not re.fullmatch(r"https://[\x21-\x7e]{3,2000}", lights_url): raise ToolError(f"--lights-url must be a web address that starts with https:// and has no spaces or accents (got {lights_url!r}).")
    if lights_url and not pack_url: raise ToolError("--lights-url needs --pack-url as well (library.yml has the Java pack first).")
    if lights_url and not lights: raise ToolError("--lights-url and --no-lights do not go together: there would be no light map to download.")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool) or not (1 <= version <= 2_000_000_000)): raise ToolError("--version must be a whole number from 1 up (it is the Bedrock pack's version: Bedrock players download the pack again when it goes up).")
    if not sets_dir.is_dir(): raise ToolError(f"{sets_dir} is not a folder. Put the DJ set files in a folder and give its name.")
    if abs(piece * 20 - round(piece * 20)) > 1e-9 or abs(overlap * 20 - round(overlap * 20)) > 1e-9 or not (2 <= piece <= 120) or not (0 <= overlap < piece / 2):
        raise ToolError("--piece and --overlap must be multiples of 0.05 seconds (whole server ticks), piece 2 to 120, overlap under half a piece")
    if bass_hz and not (40 <= bass_hz <= 1000): raise ToolError(f"--bass-hz must be between 40 and 1000 (got {bass_hz:g}), or 0 to crossfade everything.")
    ratio = 0
    if long_piece:
        ratio = round(long_piece / piece)
        if not (long_piece >= 2 * piece and long_piece <= 120) or ratio < 2 or abs(ratio * piece - long_piece) > 1e-6:
            raise ToolError(f"--long-piece must be a whole number of pieces (--piece is {piece:g} s), at least two and at most 120 s long (got {long_piece:g}), or 0 for no long pieces.")
        if java_layout != "stereo": raise ToolError("--long-piece needs the stereo Java layout (the split layout is made of two mono sounds per piece and has no long pieces).")
    bratio = 0
    if bedrock_long_piece:
        bratio = round(bedrock_long_piece / piece)
        if not (bedrock_long_piece >= 2 * piece and bedrock_long_piece <= 120) or bratio < 2 or abs(bratio * piece - bedrock_long_piece) > 1e-6:
            raise ToolError(f"--bedrock-long-piece must be a whole number of pieces (--piece is {piece:g} s), at least two and at most 120 s long (got {bedrock_long_piece:g}), or 0 for no long pieces for Bedrock.")
    ckbps = int(catchup_kbps) if catchup_kbps else max(48, kbps // 2)
    if not (48 <= ckbps <= 320): raise ToolError("--catchup-kbps must be between 48 and 320 (Ogg Vorbis cannot go lower)")
    bkbps = int(bedrock_kbps) if bedrock_kbps else kbps
    if not (48 <= bkbps <= 320): raise ToolError("--bedrock-kbps must be between 48 and 320 (Ogg Vorbis cannot go lower)")
    bckbps = int(bedrock_catchup_kbps) if bedrock_catchup_kbps else min(ckbps, bkbps)
    if not (48 <= bckbps <= 320): raise ToolError("--bedrock-catchup-kbps must be between 48 and 320 (Ogg Vorbis cannot go lower)")
    if source not in ("record", "music"): raise ToolError("--source must be record or music")
    if not (48 <= kbps <= 320): raise ToolError("--kbps must be between 48 and 320 (Ogg Vorbis cannot go lower)")
    lineup = json.loads(Path(lineup_file or HERE / "lineup.json").read_text(encoding="utf-8")) + list(extra_lineup or [])
    files = sorted(p for p in sets_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO and not p.name.startswith("."))
    if not files: raise ToolError(f"No audio files found in {sets_dir} (looking for {', '.join(sorted(AUDIO))}).")
    assigned, tests, file_warnings = match_files(files, lineup)
    if strict_ids and tests: raise ToolError("These files do not match any DJ in lineup.json: " + ", ".join(p.name for p in tests) + ". Rename them after the DJs (" + ", ".join(e["id"] for e in lineup) + ") or add the DJ to lineup.json.")
    ids = {i: p for i, (p, how) in assigned.items()}
    taken = {e["id"] for e in lineup}
    for p in tests:
        e = test_set_for(p, taken); taken.add(e["id"]); lineup.append(e); ids[e["id"]] = p
    lineup, excluded_tests = select_test_tracks(lineup, test_playlist)
    if not any(e["id"] in ids for e in lineup): raise ToolError("No audio is selected. Add a test song to test-playlist.json, add a DJ set, or choose All songs.")
    test_count = sum(bool(e.get("test")) for e in lineup)
    selection_note = f"Test playlist: {test_count} of {len(tests)} full test songs included. Scheduled DJ sets are always included; source files are unchanged."
    if not quiet:
        say(selection_note)
        for e in lineup:
            if e.get("test"): say(f"Test: {e['id']} — {ids[e['id']].name} (/wubmusic start {e['id']} 0)")
    file_notes = [(e["id"], e["name"], ids[e["id"]].name if e["id"] in ids else None, assigned[e["id"]][1] if e["id"] in assigned else ("test set" if e.get("test") else None)) for e in lineup]
    out.mkdir(parents=True, exist_ok=True)
    lm = load_lightmap() if lights else None
    if lm is not None and not all(callable(getattr(lm, name, None)) for name in ("report_markdown", "hit_report", "hit_warnings")):
        raise ToolError("Update tool/lightmap.py with the matching builder files before running this build (the per-song report helpers are missing).")
    light_sets, light_warnings = {}, []
    if lights and lm is None: light_warnings.append("lightmap.py is not next to this tool, so there are no lights (the music is built without a light map).")
    work = Path(tempfile.mkdtemp(prefix="wubmas_music_"))
    try:
        built, report = {}, []
        light_sources = {}
        for e in lineup:
            sid = e["id"]
            if sid not in ids: continue
            src = ids[sid]
            if not quiet: say(f"[{sid}] reading {src.name} ...")
            wav = work / f"{sid}.wav"
            st = to_wav(src, wav, normalize, lufs, tp)
            source_hash = hashlib.sha256()
            with src.open("rb") as source_file:
                for block in iter(lambda: source_file.read(1 << 20), b""): source_hash.update(block)
            with wave.open(str(wav), "rb") as w: dur = w.getnframes() / SR
            if dur < 2.0: raise ToolError(f"{src.name} is only {dur:.1f} seconds long.")
            count, seconds = plan(dur, piece, tail_min)
            light_sources[sid] = {"file": src.name, "sha256": source_hash.hexdigest(), "seconds": seconds}
            if not quiet: say(f"[{sid}] {dur / 60:.1f} min, cutting into {count} pieces of {piece:g} s and encoding at {kbps} kbps ...")
            cache = {}
            def shorts(kb):
                if ("s", kb) not in cache: cache[("s", kb)] = encode_pieces(wav, work / f"ogg_{kb}", sid, piece, overlap / 2, count, kb, end_fade, bass_hz=bass_hz)
                return cache[("s", kb)]
            def longs(length, kb):
                if ("l", length, kb) not in cache:
                    cache[("l", length, kb)] = encode_pieces(wav, work / f"ogg_long_{length:g}_{kb}", sid, length, overlap / 2, long_count(count, round(length / piece)), kb, end_fade, prefix="long", end_s=count * piece + overlap / 2, bass_hz=bass_hz)
                return cache[("l", length, kb)]
            # the short pieces: Bedrock's (files_) at --kbps, or at the lower catch-up bitrate when Bedrock has long pieces (only players who join in the middle hear them); Java's (files_java) likewise
            files_ = shorts(bckbps if bratio else bkbps)
            size = sum(f.stat().st_size for f in files_)
            files_l = files_r = files_long = files_java = files_blong = None
            lsize = jsize = blsize = 0
            if ratio:
                if not quiet: say(f"[{sid}] and {long_count(count, ratio)} long pieces of {long_piece:g} s ({kbps} kbps), and the short pieces for Java at {ckbps} kbps (only heard for a moment by players who join in the middle) ...")
                files_long = longs(long_piece, kbps)
                lsize = sum(f.stat().st_size for f in files_long)
            files_java = shorts(ckbps if ratio else kbps)
            jsize = sum(f.stat().st_size for f in files_java)
            if bratio:
                if not quiet: say(f"[{sid}] and {long_count(count, bratio)} long pieces of {bedrock_long_piece:g} s for Bedrock ({bkbps} kbps), and the short pieces for Bedrock at {bckbps} kbps ...")
                files_blong = longs(bedrock_long_piece, bkbps)
                blsize = sum(f.stat().st_size for f in files_blong)
            if java_layout == "split":
                if not quiet: say(f"[{sid}] and the left and the right channel apart for Java ({mono_kbps(kbps)} kbps each) ...")
                files_l = encode_pieces(wav, work / "ogg", sid, piece, overlap / 2, count, mono_kbps(kbps), end_fade, "l")
                files_r = encode_pieces(wav, work / "ogg", sid, piece, overlap / 2, count, mono_kbps(kbps), end_fade, "r")
            if lm is not None:
                if not quiet: say(f"[{sid}] finding the beats for the lights ...")
                res, lw = analyse_lights(lm, wav, sid, seconds)
                light_warnings += lw
                if res is not None: light_sets[sid] = res
            built[sid] = {"id": sid, "name": e["name"], "pieces": count, "seconds": seconds, "files": files_, "files_l": files_l, "files_r": files_r, "files_long": files_long, "files_java": files_java, "files_blong": files_blong, "long_size": lsize, "blong_size": blsize, "java_short_size": jsize, "size": size, "duration": dur, "stats": st, "source": src.name, "start": e["start"]}
            os.remove(wav)
        sets = [built[e["id"]] for e in lineup if e["id"] in built]
        vf = out / ".pack-version"
        if version is None:
            try: version = json.loads(vf.read_text())["n"] + 1
            except Exception: version = 1
        desc = f"Wubmas music{' (DEMO)' if demo else ''}, build {version}"
        jp = out / "WubmasMusic-java.zip"; bp = out / "WubmasMusic-bedrock.mcpack"
        stage = stage_entries(STAGE_DIR if stage_dir is None else stage_dir)
        sounds = java_pack(jp, sets, desc, stage, java_layout)
        defs = bedrock_pack(bp, sets, desc, source, version, "wubmas-music-demo" if demo else "wubmas-music", "Wubmas Music (demo)" if demo else "Wubmas Music")
        vf.write_text(json.dumps({"n": version}))
        brz = None
        for old in out.glob("WubmasMusic-bedrock-v*.zip"): old.unlink()      # (never leave the link pack of an earlier build next to this build's)
        if bedrock_remote:
            brz = out / f"WubmasMusic-bedrock-v{version}.zip"; brz_longest = bedrock_remote_zip(bp, brz)
        # A lights-only rebuild must use the very same source bytes and decoding settings.
        jsha = sha1(jp)
        (out / "light-sources.json").write_text(json.dumps({"format": 1, "java-sha1": jsha, "bedrock-sha1": sha1(bp), "normalize": normalize, "lufs": lufs, "tp": tp, "sets": light_sources}, indent=2) + "\n", encoding="utf-8")
        lf = out / "lights.txt"
        if lf.exists(): lf.unlink()                       # (never leave the map of an earlier build next to this build's music)
        light_report = out / "LIGHT-REPORT.md"
        if light_report.exists(): light_report.unlink()
        light_info = None
        if lm is not None:
            if light_sets: lf.write_text(lm.format_map(light_sets), encoding="utf-8")
            else: light_warnings.append("No light map was made: none of the sets gave a beat to follow. The lights stay as they are, and the music is not affected.")
            if lights_url and not light_sets: light_warnings.append("--lights-url was given but there is no light map, so library.yml has no lights entry.")
            light_info = {"sets": light_sets, "file": lf if light_sets else None, "url": lights_url if light_sets else "", "warnings": light_warnings}
            light_report.write_text(lm.report_markdown(light_sets, [s['id'] for s in sets]), encoding="utf-8")
        elif lights: light_info = {"sets": {}, "file": None, "url": "", "warnings": light_warnings}
        write_config(out / "config.yml", HERE / "config-template.yml", lineup, built, source, piece, overlap, pack_url, jsha, java_layout, long_piece if ratio else 0.0, bedrock_long_piece if bratio else 0.0, bedrock_remote=bedrock_remote)
        lurl = lights_url if light_sets else ""
        if pack_url: write_library(out / "library.yml", lineup, built, piece, overlap, pack_url, jsha, bedrock_url, sha1(bp) if bedrock_url else "", bp.stat().st_size if bedrock_url else 0, version,
                                   lights_url=lurl, lights_sha1=sha1(lf) if lurl else "", lights_bytes=lf.stat().st_size if lurl else 0, layout=java_layout, long_piece=long_piece if ratio else 0.0, bedrock_long_piece=bedrock_long_piece if bratio else 0.0)
        write_report(out / "REPORT.txt", sets, jp, bp, kbps, cap_mb, version, piece, overlap, lufs if normalize else None, lineup, pack_url, file_notes, file_warnings, 0 if demo else short_min, len(stage), light_info, java_layout, long_piece if ratio else 0.0, ckbps if ratio else None, bass_hz if (java_layout != "split" and overlap > 0) else 0, bedrock_long_piece if bratio else 0.0, bckbps if bratio else None, bkbps, bedrock_remote=brz is not None)
        with (out / "REPORT.txt").open("a", encoding="utf-8") as report_file:
            report_file.write("\n" + selection_note + "\n")
            if excluded_tests:
                report_file.write("Saved test songs excluded from this pack (edit test-playlist.json to rotate them, or choose All songs):\n")
                for e in excluded_tests: report_file.write(f"  {e['id']}: {ids[e['id']].name}\n")
        if brz is not None:
            with open(out / "REPORT.txt", "a", encoding="utf-8") as rf: rf.write(bedrock_remote_note(brz.name, brz.stat().st_size, brz_longest, "WubmasMusic.mcpack"))
        write_server_files(out, None if brz is not None else bp, lf if light_sets else None)
        return {"sets": sets, "java": jp, "bedrock": bp, "bedrock_remote": brz, "events": len(sounds), "layout": java_layout, "long_piece": long_piece if ratio else 0.0, "bedrock_long_piece": bedrock_long_piece if bratio else 0.0, "catchup_kbps": ckbps if ratio else None, "bedrock_catchup_kbps": bckbps if bratio else None, "bedrock_kbps": bkbps, "version": version, "java_mb": jp.stat().st_size / 1e6, "bedrock_mb": bp.stat().st_size / 1e6, "lineup": lineup, "lights": lf if light_sets else None, "light_sets": sorted(light_sets)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_server_files(out, bedrock_pack_path, lights_path=None):
    """server-files/plugins/... : the same files in the folders (and with the names) the server wants, so they can be dropped onto the server as they are."""
    srv = out / "server-files"
    if srv.exists(): shutil.rmtree(srv)
    cfg_dir = srv / "plugins" / "WubmasMusic"; packs_dir = srv / "plugins" / "Geyser-Spigot" / "packs"
    cfg_dir.mkdir(parents=True)
    shutil.copyfile(out / "config.yml", cfg_dir / "config.yml")
    if bedrock_pack_path is not None:       # (None: the Bedrock pack comes from a web link; the same pack in Geyser's packs folder as well would be a second copy of it)
        packs_dir.mkdir(parents=True); shutil.copyfile(bedrock_pack_path, packs_dir / "WubmasMusic.mcpack")
    if lights_path: shutil.copyfile(lights_path, cfg_dir / "lights.txt")
    return srv


def parse_start(t):
    from datetime import datetime
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try: return datetime.strptime(t, f)
        except ValueError: pass
    return None


def write_report(path, sets, jp, bp, kbps, cap_mb, version, piece, overlap, lufs, lineup=(), pack_url="", file_notes=(), file_warnings=(), short_min=10.0, stage_count=None, lights=None, layout="stereo", long_piece=0.0, catchup_kbps=None, bass_hz=0, bedrock_long_piece=0.0, bedrock_catchup_kbps=None, bedrock_kbps=None, bedrock_remote=False):
    wid = max([16] + [len(s["id"]) + 2 for s in sets] + [len(n[0]) + 2 for n in file_notes])
    L = [f"WUBMAS MUSIC - build {version}", "", f"{'set':<{wid}}{'file':<24}{'length':>8}{'pieces':>8}{'size':>9}   loudness (before -> after)"]
    warnings = list(file_warnings)
    is_test = {e["id"]: bool(e.get("test")) for e in lineup}
    for s in sets:
        st = s["stats"]
        loud = f"{st['input_i']:.1f} -> {st.get('output_i', float('nan')):.1f} LUFS, peak {st.get('output_tp', float('nan')):.1f} dBTP" if st.get("normalized") else "not levelled"
        L.append(f"{s['id']:<{wid}}{s['source'][:22]:<24}{s['duration'] / 60:>6.1f} m{s['pieces']:>8}{s['size'] / 1e6:>7.1f} MB   {loud}")
        if short_min and s["id"] in is_test and not is_test[s["id"]] and s["duration"] < short_min * 60: warnings.append(f"{s['id']}: the file {s['source']} is only {s['duration'] / 60:.1f} min long. A DJ set is usually much longer, so check that it is the right file for {s['name']}.")
        target = lufs if lufs is not None else -14.0
        if st.get("normalized"):
            if st["input_i"] < target - 12: warnings.append(f"{s['id']} was very quiet ({st['input_i']:.1f} LUFS); it has been turned up a lot, so check it for noise.")
        if s["duration"] > 3 * 3600: warnings.append(f"{s['id']} is over 3 hours.")
    by_id = {s["id"]: s for s in sets}
    order = sorted((e for e in lineup if parse_start(e["start"]) and not e.get("test")), key=lambda e: parse_start(e["start"]))
    for a, b in zip(order, order[1:]):
        s = by_id.get(a["id"])
        if not s: continue
        slot = (parse_start(b["start"]) - parse_start(a["start"])).total_seconds()
        if s["seconds"] > slot + 1:
            warnings.append(f"{a['id']} is {s['seconds'] / 60:.1f} min long but {b['id']} starts {slot / 60:.1f} min after it, so the last {s['seconds'] - slot:.0f} s of {a['id']} will be cut off when {b['id']} starts.")
    if file_notes:
        L += ["", "Which file is which:"]
        for sid, name, fname, how in file_notes:
            L.append(f"  {sid:<{wid}}{name[:28]:<30}" + (f"{fname}  ({how})" if fname else "no file yet, so this set has no music"))
        if any(h == "test set" for _, _, _, h in file_notes):
            L.append("  A test set is a file that is not a DJ's set. It never starts by itself: start it by hand with /wubmusic start <id> 0 (and /wubmusic stop).")
    jm, bm = jp.stat().st_size / 1e6, bp.stat().st_size / 1e6
    L += ["", f"Java pack:    {jm:.1f} MB   (the server limit for a resource pack is {cap_mb} MB)", f"Bedrock pack: {bm:.1f} MB   (every Bedrock player downloads this when they join, once per version)"]
    if layout == "split":
        L += ["Java layout:  split - every piece is in the Java pack as a left and a right channel (mono, "
              f"{mono_kbps(kbps)} kbps each), which the plugin plays from two speakers at the stage, so the stereo stays and the music gets quieter the further a player walks from the stage",
              "              (Bedrock players get the stereo pieces, and the fade in steps). Needs WubmasMusic 1.7.0 or newer and a stage point: /wubmusic stage set, standing at the middle of the stage and facing the dance floor."]
    else:
        L += ["Java layout:  stereo - one stereo sound per piece: the same volume everywhere for Java players (a stereo sound is not faded by the game)"]
    if long_piece:
        ls = sum(s.get("long_size", 0) for s in sets) / 1e6; js = sum(s.get("java_short_size", 0) for s in sets) / 1e6
        L += [f"Long pieces:  {long_piece:g} s ({round(long_piece / piece)} short pieces each). Java players who are in step get one long piece ({ls:.1f} MB at {kbps} kbps) for every {round(long_piece / piece)} short ones, so the tiny random timing error of a start comes {round(long_piece / piece)} times less often.",
              f"              Players who join in the middle (and every Bedrock player{", unless Bedrock has long pieces too" if bedrock_long_piece else ""}) get the short pieces of {piece:g} s, so nobody is more than {piece:g} s behind; for Java they are in the pack at {catchup_kbps} kbps ({js:.1f} MB).",
              "              Needs WubmasMusic 1.10.0 or newer (long-piece-seconds in config.yml / library.yml; both files have it already)."]
    if bedrock_long_piece:
        bls = sum(s.get("blong_size", 0) for s in sets) / 1e6; bs = sum(s.get("size", 0) for s in sets) / 1e6
        L += [f"Bedrock long pieces: {bedrock_long_piece:g} s ({round(bedrock_long_piece / piece)} short pieces each, {bls:.1f} MB at {bedrock_kbps or kbps} kbps in the Bedrock pack). Bedrock players who are in step get one for every {round(bedrock_long_piece / piece)} short ones; the short pieces for Bedrock are in the pack at {bedrock_catchup_kbps} kbps ({bs:.1f} MB).",
              "              A Bedrock sound is flat, so its volume (the fade with the distance from the stage) is set when a piece starts: with long pieces it changes only every long piece.",
              "              Needs WubmasMusic 1.11.0 or newer (bedrock-long-piece-seconds in config.yml / library.yml; both files have it already)."]
    if stage_count is not None:
        L += [f"Stage models: {stage_count} files (CDJs, mixer) are in the Java pack" if stage_count else "Stage models: none"]
        if not stage_count: warnings.append("The Java pack has no stage models (the stage_assets folder next to the tool is missing or empty), so the CDJs and the mixer show the pink-and-black missing texture in game.")
    if lights is not None:
        ls = lights["sets"]
        if ls:
            L += [f"Light map:    lights.txt, {lights['file'].stat().st_size / 1024:.0f} KB, {len(ls)} set{'s' if len(ls) != 1 else ''} (the stage lights follow the beat, the bars, the breaks and the drops)"]
            for sid in (s["id"] for s in sets):
                r = ls.get(sid)
                L.append(f"  {sid:<{wid}}" + (f"{r['bpm']:>6.1f} BPM {r['beats']:>6} beats {r['breaks']:>3} breaks {r['drops']:>3} drops   steady {r['steady'] * 100:.0f}%" if r else "no lights"))
                if r:
                    lm_report = load_lightmap()
                    if lm_report is not None:
                        L.append("    " + lm_report.hit_report(r))
                        warnings += [sid + ": " + w for w in lm_report.hit_warnings(r)]
        else:
            L += ["Light map:    none (see the warnings)"]
        warnings += lights["warnings"]
    if jm > cap_mb: warnings.append(f"The Java pack is {jm:.0f} MB, over the {cap_mb} MB limit. Use --kbps {max(48, int(kbps * cap_mb / jm * 0.9))} or fewer or shorter sets.")
    elif jm > cap_mb * 0.9: warnings.append(f"The Java pack is {jm:.0f} MB, close to the {cap_mb} MB limit.")
    if bm > 150: warnings.append(f"The Bedrock pack is {bm:.0f} MB. On a phone that is a long download at join time; consider --kbps {max(48, int(kbps * 120 / bm))}.")
    L += ["", "Warnings:"] + (["  " + w for w in warnings] if warnings else ["  none"])
    L += ["", "Put these on the server:",
          "  1. Drag the folder 'server-files/plugins' (it is next to this report) onto the server's main folder and choose merge / replace.",
          *(["     It holds config.yml (and lights.txt) already in the right folder (plugins/WubmasMusic/) with the right names. It has no Bedrock pack: the Bedrock pack comes from a web link (see the end of this report).",
             "     If an earlier build's WubmasMusic.mcpack is in plugins/Geyser-Spigot/packs on the server, delete it. Then restart the server."] if bedrock_remote else
            ["     It holds config.yml and the Bedrock pack already in the right folders (plugins/WubmasMusic/ and plugins/Geyser-Spigot/packs/) with the right names,",
             "     and it replaces the old Bedrock pack, so there is nothing to rename or delete. Then restart the server."]),
          "  2. WubmasMusic-java.zip -> upload it where a direct download link works (a new GitHub release; use a file name you have not used before, or players keep their cached old pack).",
          f"     The pack's SHA-1 is {sha1(jp)} and config.yml already has it.",
          (f"     config.yml also has the link {pack_url}, so the file has to be at exactly that address." if pack_url
           else "     Then put the link in plugins/WubmasMusic/config.yml, under resource-pack:, as  url: \"<that link>\"  (it is empty now, so the plugin sends no pack yet), and run /wubmusic reload."),
          "     Leave resource-pack= and resource-pack-sha1= EMPTY in server.properties (with the server stopped): the plugin sends the Java pack a moment after players join,",
          "     so nobody waits for it in the join screen. A player who has the same pack already (same SHA-1) downloads nothing.",
          *( ["  3. lights.txt -> upload it where a direct download link works, at exactly " + lights["url"] + " (the same website as library.yml; /wubmusic update downloads it and checks it against the SHA-1 in library.yml).",
              "     It is also in server-files/plugins/WubmasMusic/, so step 1 already puts this build's light map on the server."] if lights and lights["file"] and lights["url"] else
             ["  3. lights.txt is in server-files/plugins/WubmasMusic/, so step 1 puts it on the server. (Build with --lights-url so that /wubmusic update brings the light map too.)"] if lights and lights["file"] else []),
          "", f"Timing: pieces of {piece:g} s with {overlap:g} s overlap" + (f", long pieces of {long_piece:g} s" if long_piece else "") + ". config.yml must have the same piece-seconds, overlap-seconds" + (" and long-piece-seconds" if long_piece else "") + " (it does unless you changed them)." + (f" The joins switch the sound below {bass_hz:g} Hz instead of crossfading it, to keep the dips short (nothing to set for that)." if bass_hz else "")]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def demo_track(seconds=120, bpm=128):
    """A made-up track with a beep every 10 seconds, for testing the whole chain without anyone's music."""
    import numpy as np
    n = int(SR * seconds); t = np.arange(n) / SR; beat = 60.0 / bpm
    L = np.zeros(n); R = np.zeros(n)
    def add(sig, start, pan=0.0):
        i = int(start * SR)
        if i >= n: return
        j = min(n, i + len(sig))
        L[i:j] += sig[:j - i] * (1 - max(0, pan)); R[i:j] += sig[:j - i] * (1 + min(0, pan))
    kt = np.arange(int(SR * 0.28)) / SR
    kick = np.sin(2 * np.pi * np.cumsum(45 + 90 * np.exp(-kt * 28)) / SR) * np.exp(-kt * 9) * 0.95
    rng = np.random.default_rng(7); ht = np.arange(int(SR * 0.06)) / SR
    hat = np.diff(rng.standard_normal(len(ht) + 1)) * np.exp(-ht * 80) * 0.22
    b = 0.0
    while b < seconds: add(kick, b); add(hat, b + beat / 2); b += beat
    bar = beat * 4
    sub = (np.sin(2 * np.pi * 55 * t) + 0.35 * np.sin(2 * np.pi * 110 * t + 3 * np.sin(2 * np.pi * 2.0 * t))) * 0.33 * (1 - 0.85 * np.exp(-((t % beat)) * 12)) * (t >= 4 * bar)
    L += sub; R += sub
    notes = [220.0, 261.63, 329.63, 440.0, 329.63, 261.63]; step = beat / 4; k = 0; s = 8 * bar; nt = np.arange(int(SR * step * 1.6)) / SR
    while s < seconds - 1:
        f = notes[k % len(notes)]
        add((np.sin(2 * np.pi * f * nt) + 0.4 * np.sin(2 * np.pi * 2 * f * nt)) * np.exp(-nt * 11) * 0.13, s, pan=-0.8 if k % 2 == 0 else 0.8); k += 1; s += step
    def beep(freq, dur=0.12):
        bt = np.arange(int(SR * dur)) / SR
        return np.sin(2 * np.pi * freq * bt) * np.minimum(1, np.minimum(bt * 400, (dur - bt) * 400)) * 0.35
    for sec in range(10, seconds - 1, 10):
        for c in range(sec // 10 % 6 + 1): add(beep(1000 + 100 * (sec // 10 % 6 + 1)), sec + 0.05 + 0.25 * c)
    peak = max(np.abs(L).max(), np.abs(R).max())
    return np.stack([L, R], axis=1) / peak * 0.89


def make_demo(folder, seconds=120):
    import numpy as np
    x = demo_track(seconds)
    p = Path(folder) / "soundcheck.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Turn DJ set files into the Wubmas music resource packs and plugin config.")
    ap.add_argument("sets", nargs="?", help="folder with one audio file per set (file name = set id, e.g. infrno.wav)")
    ap.add_argument("--out", default="build", help="where to write the packs (default: build)")
    ap.add_argument("--kbps", type=int, default=96, help="Ogg Vorbis bitrate; 96 is about 150 MB for seven half-hour sets, 128 about 200 MB (default 96)")
    ap.add_argument("--lufs", type=float, default=-14.0, help="loudness every set is levelled to (default -14)")
    ap.add_argument("--no-normalize", action="store_true", help="keep each file's own loudness")
    ap.add_argument("--source", default="record", help="volume slider: record (jukebox, default) or music")
    ap.add_argument("--piece", type=float, default=15.0, help="seconds per piece (default 15; must match piece-seconds in the plugin config)")
    ap.add_argument("--overlap", type=float, default=0.3, help="seconds of crossfade between pieces (default 0.3)")
    ap.add_argument("--lineup", help="a lineup.json other than the one next to this script")
    ap.add_argument("--cap-mb", type=int, default=250, help="size limit to warn about for the Java pack (default 250)")
    ap.add_argument("--pack-url", default="", help="the web address the Java pack will be uploaded to; it goes into config.yml (with the pack's SHA-1) so there is nothing to paste in later")
    ap.add_argument("--bedrock-url", default="", help="the web address the Bedrock pack will be uploaded to (library.yml gets it with the pack's SHA-1; /wubmusic update downloads it from there into Geyser's packs folder)")
    ap.add_argument("--version", type=int, default=None, help="the Bedrock pack's version number (default: one more than the last build in the --out folder). A build server that starts from nothing every time must pass a number that only goes up, for example its run number")
    ap.add_argument("--stage-assets", default=None, help="folder with the CDJ/mixer models and textures for the Java pack, laid out as in a resource pack (default: stage_assets next to this tool)")
    ap.add_argument("--short-minutes", type=float, default=10.0, help="warn when a DJ's file is shorter than this many minutes, because it is probably a song and not a set (0 = never warn)")
    ap.add_argument("--strict-ids", action="store_true", help="stop with an error when a file is not one of the DJs in lineup.json (instead of making it a test set that never starts by itself)")
    ap.add_argument("--lights-url", default="", help="the web address the light map (lights.txt) will be uploaded to, on the same website as the packs (library.yml gets it with the SHA-1; /wubmusic update downloads it)")
    ap.add_argument("--no-lights", action="store_true", help="do not find the beats for the stage lights (saves a few seconds per set; there is no light map then)")
    ap.add_argument("--long-piece", type=float, default=0.0, help="also cut the Java pack into long pieces of this many seconds (a whole number of --piece, for example --piece 6 --long-piece 30): players who are in step get a long piece for every few short ones, so the tiny timing error of a start (a dip, a skipped moment) comes that many times less often, while players who join in the middle get the short pieces and are never more than one short piece behind. 0 = no long pieces (default). Needs WubmasMusic 1.10.0")
    ap.add_argument("--bedrock-long-piece", type=float, default=0.0, help="also cut the Bedrock pack into long pieces of this many seconds (a whole number of --piece; it may be shorter than --long-piece because phones and consoles are not as strong): Bedrock players who are in step get a long piece for every few short ones, so they have joins that many times less often, while Bedrock players who join in the middle get the short pieces. 0 = none (default). Needs WubmasMusic 1.11.0")
    ap.add_argument("--bedrock-kbps", type=int, default=None, help="the bitrate of the Bedrock pack (default: the same as --kbps). Phones download the whole pack when they join: a lower bitrate makes it smaller, and every Bedrock sound is played flat (no stage distance in the sound itself), so phone speakers and earbuds lose little. 48 is the lowest Ogg Vorbis can do in stereo")
    ap.add_argument("--bedrock-remote", action="store_true", help="also write the Bedrock pack as WubmasMusic-bedrock-vN.zip for a web link (Geyser's resource-pack-urls in its config.yml): the phones and consoles then download it from that link and not from the server, which matters for a big crowd. The link must answer with the type application/zip and the exact length")
    ap.add_argument("--bedrock-catchup-kbps", type=int, default=None, help="the bitrate of the short pieces in the Bedrock pack when it has long pieces (default: the same as --catchup-kbps, or --bedrock-kbps if that is lower)")
    ap.add_argument("--catchup-kbps", type=int, default=None, help="the bitrate of the short pieces in the Java pack when there are long pieces (only players who join in the middle hear them, for a moment; default: half of --kbps, at least 48). The Bedrock pack has its own bitrate: --bedrock-kbps (default --kbps)")
    ap.add_argument("--bass-hz", type=float, default=BASS_HZ, help=f"where two pieces meet, the sound below this pitch is switched from one piece to the next in a few milliseconds and only the sound above it is crossfaded (default {BASS_HZ:g}). The game starts every piece a few milliseconds off, and two crossfaded copies of the bass then cancel for a moment (the 'dip'); a switch makes that rarer and shorter. 0 = crossfade everything, as before. Does not change anything in config.yml")
    ap.add_argument("--java-split", action="store_true", help="make the Java pack with the left and the right channel apart (two mono sounds per piece), which the plugin plays from two speakers at the stage so the music fades with the distance. The default is one stereo sound per piece: the split layout can cancel the bass for a moment when the two sounds start a few milliseconds apart, so it is not used for the show. No long pieces with it")
    ap.add_argument("--java-stereo", action="store_true", help="the same as the default (one stereo sound per piece); kept so that older commands still work")
    ap.add_argument("--demo", action="store_true", help="build a 2 minute made-up sound check set instead of reading a folder (needs numpy)")
    playlist = ap.add_mutually_exclusive_group()
    playlist.add_argument("--test-playlist", default=None, help="JSON file listing the test_set_ids to package; default: the repository's test-playlist.json. DJ sets are always included")
    playlist.add_argument("--all-test-tracks", action="store_true", help="include every full test song; makes a larger download for everyone")
    a = ap.parse_args(argv)
    try:
        if a.java_split and a.java_stereo: ap.error("--java-split and --java-stereo do not go together")
        ffmpeg_path()
        tmp = None
        if a.demo:
            try: import numpy  # noqa
            except ImportError: raise ToolError("--demo needs numpy (pip install numpy)")
            tmp = tempfile.mkdtemp(prefix="wubmas_demo_"); make_demo(tmp); a.sets = tmp
        if not a.sets: ap.error("give the folder with the set files (or --demo)")
        r = build(a.sets, a.out, kbps=a.kbps, piece=a.piece, overlap=a.overlap, lufs=a.lufs, normalize=not a.no_normalize, lineup_file=a.lineup, source=a.source, cap_mb=a.cap_mb,
                  extra_lineup=[{"id": "soundcheck", "name": "Sound check", "start": "2026-01-01 12:00"}] if a.demo else None, demo=a.demo, pack_url=a.pack_url.strip(), bedrock_url=a.bedrock_url.strip(), version=a.version, strict_ids=a.strict_ids, short_min=a.short_minutes, stage_dir=a.stage_assets, lights=not a.no_lights, lights_url=a.lights_url.strip(), java_layout="split" if a.java_split else "stereo", long_piece=a.long_piece, catchup_kbps=a.catchup_kbps, bass_hz=a.bass_hz, bedrock_long_piece=a.bedrock_long_piece, bedrock_catchup_kbps=a.bedrock_catchup_kbps, bedrock_kbps=a.bedrock_kbps, bedrock_remote=a.bedrock_remote,
                  test_playlist=None if (a.all_test_tracks or a.demo) else (a.test_playlist or HERE.parent / "test-playlist.json"))
        if tmp: shutil.rmtree(tmp, ignore_errors=True)
    except ToolError as e:
        print("\nERROR: " + str(e), file=sys.stderr); return 1
    say(); say((Path(a.out) / "REPORT.txt").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
