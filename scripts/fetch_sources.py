#!/usr/bin/env python3
"""Gets the music files for a build into one folder.

  fetch_sources.py REPO DEST [--draft "Wubmas music files"] [--folder music]

1. The private DRAFT release called "Wubmas music files" of the repository: every audio file attached to it is downloaded. (A draft release is only
   visible to people who can push to the repository, so the DJs' files are not public. Its files can be up to 2 GB each, and you add them by dragging
   them onto the release page in the browser.)
2. The folder "music" of the repository, if it has one (for files that were added with git; they are public if the repository is).
Needs the GitHub command line tool (gh) and GH_TOKEN in the environment, which GitHub Actions provides."""
import argparse, json, os, re, subprocess, sys
from pathlib import Path

AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".aif", ".aiff", ".opus", ".wma", ".mp4", ".mkv", ".webm"}
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ ()+\-]{0,200}")


class Fail(Exception):
    pass


def fail_text(msg):
    if os.environ.get("GITHUB_ACTIONS"): print("::error title=Wubmas music::" + msg.replace("\n", "%0A"))
    print("ERROR: " + msg, file=sys.stderr)


def gh(args, to_file=None):
    try:
        p = subprocess.run(["gh"] + args, stdout=to_file if to_file else subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise Fail("The GitHub command line tool (gh) is not installed here.")
    if p.returncode != 0:
        raise Fail(f"gh {' '.join(args[:3])} failed: {p.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    return p.stdout


def all_releases(repo):
    raw = gh(["api", "--paginate", f"repos/{repo}/releases?per_page=100"]).decode("utf-8", "replace")
    dec, i, items = json.JSONDecoder(), 0, []
    while True:                                           # --paginate prints one JSON array per page, back to back
        while i < len(raw) and raw[i].isspace(): i += 1
        if i >= len(raw): break
        try: obj, i = dec.raw_decode(raw, i)
        except ValueError: raise Fail("GitHub's list of releases could not be read.")
        items += obj if isinstance(obj, list) else [obj]
    return items


def find_draft(items, name):
    drafts = [r for r in items if r.get("draft") and (r.get("name") or "").strip().lower() == name.strip().lower()]
    if not drafts:
        seen = [f"{(r.get('name') or r.get('tag_name') or '?')!r}{' (draft)' if r.get('draft') else ''}" for r in items][:12]
        return None, seen
    return drafts[0], []            # GitHub lists the newest first


def fetch_draft(repo, name, dest, say=print):
    rel, seen = find_draft(all_releases(repo), name)
    if rel is None:
        say(f'No draft release called "{name}" in {repo}' + (f" (releases I can see: {', '.join(seen)})" if seen else " (this repository has no releases I can see)") + ".")
        return []
    got = []
    for a in sorted(rel.get("assets") or [], key=lambda a: a.get("name", "")):
        n = a.get("name", "")
        if a.get("state") not in (None, "uploaded"): say(f"  skipped {n}: its upload is not finished"); continue
        if Path(n).suffix.lower() not in AUDIO: say(f"  skipped {n}: not an audio file"); continue
        if not SAFE_NAME.fullmatch(n): say(f"  skipped {n!r}: the name has characters I will not use as a file name"); continue
        target = Path(dest) / n; part = Path(dest) / (n + ".part")
        with open(part, "wb") as f: gh(["api", "-H", "Accept: application/octet-stream", f"repos/{repo}/releases/assets/{int(a['id'])}"], to_file=f)
        if a.get("size") is not None and part.stat().st_size != a["size"]:
            part.unlink(missing_ok=True); raise Fail(f"{n} came down as {part.stat().st_size if part.exists() else 0} bytes but should be {a['size']}. Run the workflow again.")
        os.replace(part, target); got.append(target); say(f"  {n}  ({target.stat().st_size / 1e6:.1f} MB) from the draft release")
    return got


def copy_folder(folder, dest, say=print):
    got = []
    f = Path(folder)
    if not f.is_dir(): return got
    for p in sorted(f.iterdir()):
        if p.is_file() and p.suffix.lower() in AUDIO and not p.name.startswith("."):
            t = Path(dest) / p.name
            if t.exists(): say(f"  {p.name}: the draft release has a file with this name already, so the one in the {folder} folder is ignored"); continue
            t.write_bytes(p.read_bytes()); got.append(t); say(f"  {p.name}  ({t.stat().st_size / 1e6:.1f} MB) from the {folder} folder")
    return got


def main(argv=None):
    ap = argparse.ArgumentParser(description="Get the music files for a build.")
    ap.add_argument("repo"); ap.add_argument("dest"); ap.add_argument("--draft", default="Wubmas music files"); ap.add_argument("--folder", default="music")
    a = ap.parse_args(argv)
    Path(a.dest).mkdir(parents=True, exist_ok=True)
    try:
        print(f'Looking for the draft release "{a.draft}" ...')
        got = fetch_draft(a.repo, a.draft, a.dest)
        got += copy_folder(a.folder, a.dest)
    except Fail as e:
        fail_text(str(e)); return 1
    if not got:
        fail_text(f'There is no music to build. Put the sets (dxbuddha.mp3, yungsiete.mp3 ...) in a draft release called "{a.draft}" (Releases, Draft a new release, drag the files in, Save draft), then run this again.')
        return 1
    print(f"{len(got)} file(s) ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
