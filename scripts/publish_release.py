#!/usr/bin/env python3
"""Publishes a finished build as a GitHub release and removes the old builds.

  publish_release.py REPO TAG BUILD_DIR [--target COMMIT] [--keep 3]

BUILD_DIR is what make_music_packs.py wrote. The release gets the Java pack, the Bedrock pack, library.yml, REPORT.txt and, when library.yml has a light map, lights.txt, and becomes the "latest" release,
so that  https://github.com/REPO/releases/latest/download/library.yml  is always the newest build. Builds older than the newest --keep are deleted
(their tags too): only releases whose tag looks like music-<number> are ever touched. Needs gh and GH_TOKEN."""
import argparse, json, os, re, subprocess, sys, tempfile
from pathlib import Path

FILES = ["WubmasMusic-java.zip", "WubmasMusic-bedrock.mcpack", "library.yml", "REPORT.txt"]
TAG = re.compile(r"music-(\d+)")


class Fail(Exception):
    pass


def gh(args, check=True):
    try: p = subprocess.run(["gh"] + args, capture_output=True)
    except FileNotFoundError: raise Fail("The GitHub command line tool (gh) is not installed here.")
    if check and p.returncode != 0: raise Fail(f"gh {' '.join(args[:2])} failed: {p.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    return p


def old_builds(listing, keep, current):
    """Tags to delete: music-N releases (not drafts) other than the newest `keep` and never the one being published."""
    mine = sorted(((int(TAG.fullmatch(r["tagName"]).group(1)), r["tagName"]) for r in listing if TAG.fullmatch(r.get("tagName", "")) and not r.get("isDraft")), reverse=True)
    keepers = {t for _, t in mine[:keep]} | {current}
    return [t for _, t in mine if t not in keepers]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Publish a build as a release.")
    ap.add_argument("repo"); ap.add_argument("tag"); ap.add_argument("build"); ap.add_argument("--target", default=""); ap.add_argument("--keep", type=int, default=3)
    a = ap.parse_args(argv)
    try:
        if not TAG.fullmatch(a.tag): raise Fail(f"The tag must look like music-12 (got {a.tag!r}).")
        if a.keep < 1: raise Fail("--keep must be at least 1.")
        b = Path(a.build); missing = [f for f in FILES if not (b / f).is_file()]
        if missing: raise Fail("The build folder has no " + ", ".join(missing) + ".")
        lib = (b / "library.yml").read_text(encoding="utf-8")
        if f"releases/download/{a.tag}/" not in lib: raise Fail(f"library.yml does not point at the release {a.tag}, so the server would download the wrong files.")
        files = list(FILES)
        if re.search(r"^lights:", lib, re.M):        # (the build has a light map, and library.yml tells the server where to get it)
            if not (b / "lights.txt").is_file(): raise Fail("library.yml has a light map but the build folder has no lights.txt, so the server's update would find nothing at that address.")
            if f"releases/download/{a.tag}/lights.txt" not in lib: raise Fail(f"library.yml's light map is not at the release {a.tag}, so the server would download the wrong file.")
            files.append("lights.txt")
        elif (b / "lights.txt").is_file(): print("Note: the build has a lights.txt but library.yml does not point at it (no --lights-url), so it is not published.")
        gh(["release", "delete", a.tag, "--repo", a.repo, "--yes", "--cleanup-tag"], check=False)          # a re-run of the same build replaces its release
        notes = f"Build {a.tag.split('-')[1]} of the Wubmas music.\n\nOn the server: /wubmusic update\n\n" + (b / "REPORT.txt").read_text(encoding="utf-8")[:6000]
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as nf: nf.write(notes); nfp = nf.name
        cmd = ["release", "create", a.tag] + [str(b / f) for f in files] + ["--repo", a.repo, "--title", f"Wubmas music build {a.tag.split('-')[1]}", "--notes-file", nfp, "--latest"]
        if a.target: cmd += ["--target", a.target]
        try: gh(cmd)
        finally: os.unlink(nfp)
        print(f"Published {a.tag}.")
        listing = json.loads(gh(["release", "list", "--repo", a.repo, "--limit", "100", "--json", "tagName,isDraft"]).stdout.decode() or "[]")
        for t in old_builds(listing, a.keep, a.tag):
            r = gh(["release", "delete", t, "--repo", a.repo, "--yes", "--cleanup-tag"], check=False)
            print(("Removed the old build " if r.returncode == 0 else "Could not remove the old build ") + t)
        link = f"https://github.com/{a.repo}/releases/latest/download/library.yml"
        summ = os.environ.get("GITHUB_STEP_SUMMARY")
        text = f"### Wubmas music build {a.tag.split('-')[1]} is ready\n\nOn the server: `/wubmusic update`\n\nThe link for `library.url` in the plugin's config.yml (only needed once):\n\n`{link}`\n"
        if summ: Path(summ).open("a", encoding="utf-8").write(text + "\n")
        print("\nOn the server run /wubmusic update.\nlibrary.url for config.yml (only needed once): " + link)
    except Fail as e:
        if os.environ.get("GITHUB_ACTIONS"): print("::error title=Wubmas music::" + str(e))
        print("ERROR: " + str(e), file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
