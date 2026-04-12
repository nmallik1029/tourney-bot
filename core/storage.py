import json
import os
from pathlib import Path

GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
GIST_ID = os.environ.get("GITHUB_GIST_ID", "")
TOURNAMENTS_FILE = Path("tournaments.json")


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def load_tournaments() -> dict:
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                gist = json.loads(resp.read().decode())
                content = gist["files"]["tournaments.json"]["content"]
                data = json.loads(content)
                print(f"[Storage] Loaded {len(data)} tournaments from Gist")
                return data
        except Exception as e:
            print(f"[Storage] Failed to load from Gist: {e}")
    if TOURNAMENTS_FILE.exists():
        with open(TOURNAMENTS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_tournaments():
    data_str = json.dumps(tournaments, indent=2, default=str)
    with open(TOURNAMENTS_FILE, "w") as f:
        f.write(data_str)
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            payload = json.dumps({"files": {"tournaments.json": {"content": data_str}}}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=payload,
                headers={**_gist_headers(), "Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"[Storage] Failed to save to Gist: {e}")


def load_config() -> dict:
    config_file = Path("config.json")
    if config_file.exists():
        with open(config_file, "r") as f:
            return json.load(f)
    return {}


def save_config(data: dict):
    with open("config.json", "w") as f:
        json.dump(data, f, indent=2)


# ── Shared state ──────────────────────────────────────────────────────────────
tournaments: dict[str, dict] = load_tournaments()
active_matches: dict[str, dict] = {}
pending_registrations: dict[int, dict] = {}
_dashboard_tokens: set[str] = set()
