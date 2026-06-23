"""Per-player custom rank-card backgrounds.

Stored as base64 JPEGs in a SEPARATE gist file (rank_backgrounds.json) so they never
bloat the main pug_data.json that loads every session. Keyed by "<guild_id>:<user_id>"
because player data is per-guild. Each image is cover-cropped to the card size and
quality-compressed on upload so the stored blob stays small and gist-friendly.
"""

import base64
import io
import json
import os
from pathlib import Path

from PIL import Image

# Card image size the backgrounds are cropped to (must match pug/graph.py CARD_W/H).
CARD_W, CARD_H = 820, 460

BG_FILE = Path("rank_backgrounds.json")
GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
GIST_ID = os.environ.get("GITHUB_GIST_ID", "")

# Keep each stored image comfortably small for the gist (base64 inflates ~33%).
_MAX_STORED_BYTES = 220_000


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def _load() -> dict:
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers()
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                gist = json.loads(resp.read().decode())
                files = gist.get("files", {})
                if "rank_backgrounds.json" in files:
                    return json.loads(files["rank_backgrounds.json"]["content"])
        except Exception as e:
            print(f"[Backgrounds] Failed to load from Gist: {e}")
    if BG_FILE.exists():
        try:
            with open(BG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Backgrounds] Failed to load rank_backgrounds.json: {e}")
    return {}


_store: dict[str, str] = _load()


def _save():
    data_str = json.dumps(_store)
    with open(BG_FILE, "w") as f:
        f.write(data_str)
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            payload = json.dumps({"files": {"rank_backgrounds.json": {"content": data_str}}}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=payload,
                headers={**_gist_headers(), "Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as e:
            print(f"[Backgrounds] Failed to save to Gist: {e}")


def _key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def _cover_crop(img: Image.Image) -> Image.Image:
    """Scale + center-crop the image to exactly CARD_W x CARD_H (fills, no distortion)."""
    img = img.convert("RGB")
    src_w, src_h = img.size
    scale = max(CARD_W / src_w, CARD_H / src_h)
    new_w, new_h = max(1, int(src_w * scale)), max(1, int(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - CARD_W) // 2
    top = (new_h - CARD_H) // 2
    return img.crop((left, top, left + CARD_W, top + CARD_H))


def set_background(guild_id: int, user_id: int, image_bytes: bytes) -> None:
    """Crop, compress, and store a player's background. Raises ValueError if the bytes
    aren't a usable image."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as e:
        raise ValueError(f"Not a readable image: {e}")

    cropped = _cover_crop(img)
    # Step the JPEG quality down until it fits the size budget.
    data = None
    for quality in (85, 75, 65, 55, 45):
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= _MAX_STORED_BYTES:
            break
    _store[_key(guild_id, user_id)] = base64.b64encode(data).decode("ascii")
    _save()


def get_background_bytes(guild_id: int, user_id: int) -> bytes | None:
    """Decoded JPEG bytes for a player's background, or None if they have none set."""
    b64 = _store.get(_key(guild_id, user_id))
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def clear_background(guild_id: int, user_id: int) -> bool:
    """Remove a player's background. Returns True if one was actually set."""
    existed = _store.pop(_key(guild_id, user_id), None) is not None
    if existed:
        _save()
    return existed
