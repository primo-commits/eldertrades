"""
Unified Alpaca credential loading.

The original code had two incompatible schemes: the paper trader wanted
ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET (or a file with API_KEY=), while the
backtester wanted ALPACA_API_KEY / ALPACA_SECRET_KEY. One alpaca_keys.txt could
not satisfy both. This accepts every spelling.

Crucially this does NOT call sys.exit() at import time -- the original did, which
made the module impossible to import or unit-test without live credentials.
"""
from __future__ import annotations

import os
from pathlib import Path

_KEY_NAMES    = ("ALPACA_API_KEY", "ALPACA_PAPER_KEY", "APCA_API_KEY_ID", "API_KEY")
_SECRET_NAMES = ("ALPACA_SECRET_KEY", "ALPACA_PAPER_SECRET", "APCA_API_SECRET_KEY", "API_SECRET")


class MissingCredentials(RuntimeError):
    """Raised on demand -- never at import time."""


def _read_keyfile(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    # utf-8-sig: Notepad on Windows writes a BOM that otherwise corrupts the
    # first key name.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip().upper()] = value.strip().strip('"').strip("'")
    return out


def describe_source(keyfile: str | os.PathLike | None = None) -> dict:
    """
    Where would credentials come from right now? Environment variables take
    precedence over the key file, so a stale env var left over from an earlier
    setup will silently override alpaca_keys.txt and point the bot at a
    different account. This reports the winner and any shadowed sources.
    """
    path = Path(keyfile) if keyfile else Path(__file__).resolve().parent.parent / "alpaca_keys.txt"
    file_vals = _read_keyfile(path)
    env_hits = [n for n in _KEY_NAMES if os.environ.get(n)]
    file_hits = [n for n in _KEY_NAMES if file_vals.get(n)]
    return {
        "env_vars_set": env_hits,
        "file_path": str(path),
        "file_exists": path.is_file(),
        "file_keys": file_hits,
        "winner": ("environment variable " + env_hits[0]) if env_hits
                  else (f"file {path.name}" if file_hits else "nothing found"),
        "shadowed": bool(env_hits and file_hits),
    }


def load_keys(keyfile: str | os.PathLike | None = None) -> tuple[str, str]:
    """
    Resolve (api_key, api_secret). Environment wins over the key file.

    Raises MissingCredentials with an actionable message if nothing is found.
    """
    search = dict(os.environ)
    path = Path(keyfile) if keyfile else Path(__file__).resolve().parent.parent / "alpaca_keys.txt"
    for k, v in _read_keyfile(path).items():
        search.setdefault(k, v)

    key    = next((search[n] for n in _KEY_NAMES    if search.get(n)), None)
    secret = next((search[n] for n in _SECRET_NAMES if search.get(n)), None)

    placeholder_markers = ("your_key", "your_secret", "paste", "xxxx", "PKxxxx")
    if key and any(m.lower() in key.lower() for m in placeholder_markers):
        raise MissingCredentials(
            "alpaca_keys.txt still contains the placeholder text.\n\n"
            "  Open alpaca_keys.txt in Notepad and replace the example values\n"
            "  with your real Alpaca PAPER key and secret, then save.\n"
            "  Get them at https://app.alpaca.markets/paper/dashboard/overview\n"
            "  (Home -> API Keys -> Generate)"
        )

    if not key or not secret:
        raise MissingCredentials(
            "No Alpaca API credentials found.\n"
            f"  Looked in env vars {_KEY_NAMES} / {_SECRET_NAMES}\n"
            f"  and in {path}\n\n"
            "  Fix: create that file with\n"
            "    ALPACA_API_KEY=your_key_here\n"
            "    ALPACA_SECRET_KEY=your_secret_here\n"
            "  Paper keys come from https://app.alpaca.markets/paper/dashboard/overview\n"
            "  (Home -> API Keys -> Generate). Paper and live keys are NOT interchangeable."
        )
    return key, secret


def has_keys(keyfile: str | os.PathLike | None = None) -> bool:
    try:
        load_keys(keyfile)
        return True
    except MissingCredentials:
        return False
