"""Remove source music outside the owner's selection and retire old mixed packs.

Uses the normal GitHub Actions repository token. Old pack deletion is restricted
to the exact assets inventoried before the artist cleanup; future builds are kept.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from fetch_sources import AUDIO, Fail, all_releases
from music_policy import allowed_source, artist_pattern, pending_source

# Existing public audio archives inventoried for the cleanup on 2026-11-19.
OLD_PACKS = {
    757953354: "WubmasMusic-bedrock.mcpack",
    757953355: "WubmasMusic-java.zip",
    756857680: "WubmasMusic-bedrock.mcpack",
    756857685: "WubmasMusic-java.zip",
    756724260: "WubmasMusic-bedrock.mcpack",
    756724257: "WubmasMusic-java.zip",
    728952107: "WubmasMusicDemo-java.zip",
}


def api(repo, asset_id, method="GET", missing_ok=False):
    endpoint = f"repos/{repo}/releases/assets/{int(asset_id)}"
    p = subprocess.run(["gh", "api", "--method", method, endpoint], capture_output=True)
    if p.returncode:
        error = p.stderr.decode("utf-8", "replace")
        if missing_ok and "HTTP 404" in error:
            return None
        raise Fail(f"Could not {method} music asset {asset_id}: {error[-400:]}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def source_removals(releases):
    """Only source audio is considered; documents, images and unrelated drafts stay."""
    result = []
    for release in releases:
        if release.get("draft") and (release.get("name") or "").strip().casefold() != "wubmas music files":
            continue
        for asset in release.get("assets") or []:
            name = asset.get("name", "")
            if Path(name).suffix.casefold() in AUDIO and not allowed_source(name) and not pending_source(name):
                result.append(asset)
    return result


def clean_sources(repo):
    removals = source_removals(all_releases(repo))
    for asset in removals:
        api(repo, asset["id"], "DELETE", missing_ok=True)
        print("Removed source:", repr(asset["name"]))
    return ["Removed source: " + repr(a["name"]) for a in removals]


def clean_old_packs(repo):
    messages = []
    for asset_id, expected_name in OLD_PACKS.items():
        asset = api(repo, asset_id, missing_ok=True)
        if asset is None:
            continue
        if asset.get("name") != expected_name:
            raise Fail(f"Old music asset {asset_id} has an unexpected name; nothing was deleted for that asset.")
        api(repo, asset_id, "DELETE", missing_ok=True)
        message = f"Retired old pack: {expected_name} (asset {asset_id})"
        messages.append(message)
        print(message)
    return messages


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sources", action="store_true")
    mode.add_argument("--old-packs", action="store_true")
    args = ap.parse_args()
    try:
        artist_pattern()  # Validate the keep list before making any changes.
        messages = clean_sources(args.repo) if args.sources else clean_old_packs(args.repo)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("\n### Music cleanup\n\n" + ("\n".join("- " + m for m in messages) or "No additional files needed removal.") + "\n")
    except (Fail, OSError, ValueError) as e:
        print("ERROR:", str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
