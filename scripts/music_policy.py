"""The owner's artist selection for uploaded music; filenames do not prove rights."""
import functools
import json
import re
from pathlib import Path

POLICY = Path(__file__).resolve().parents[1] / "music-artists.json"


@functools.lru_cache(maxsize=1)
def artist_pattern():
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    artists = data.get("keep_artists") if isinstance(data, dict) else None
    if not isinstance(artists, list) or not artists or any(not isinstance(a, str) or not a.strip() for a in artists):
        raise ValueError("music-artists.json must contain a nonempty keep_artists list.")
    alternatives = []
    for artist in artists:
        words = re.findall(r"[a-z0-9]+", artist.casefold())
        if not words:
            raise ValueError("Each kept artist must contain letters or numbers.")
        alternatives.append(r"[\W_]*".join(re.escape(word) for word in words))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alternatives) + r")(?![a-z0-9])", re.I)


def allowed_source(name):
    return bool(artist_pattern().search(Path(name).stem))


def pending_source(name):
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    pending = data.get("pending_sources", [])
    if not isinstance(pending, list) or any(not isinstance(p, str) for p in pending):
        raise ValueError("pending_sources must be a list of exact filenames.")
    return Path(name).name.casefold() in {p.casefold() for p in pending}
