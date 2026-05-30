"""Credentials file management — ~/.config/inferroute/credentials.

Pattern follows `gh auth`: a small file with one key per line, mode 600,
read on every CLI invocation.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


DEFAULT_API_URL = "https://api.inferroute.ai"
CREDS_FILE = Path(
    os.environ.get("INFERROUTE_CREDS_FILE")
    or str(Path.home() / ".config" / "inferroute" / "credentials")
)


@dataclass
class Credentials:
    api_url: str
    api_key: str

    @property
    def is_valid(self) -> bool:
        return bool(self.api_key)


def _parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def load() -> Credentials:
    """Resolve creds. Precedence: env vars → creds file → empty."""
    file_vals = _parse(CREDS_FILE)
    return Credentials(
        api_url=(
            os.environ.get("INFERROUTE_API_URL")
            or file_vals.get("INFERROUTE_API_URL")
            or DEFAULT_API_URL
        ).rstrip("/"),
        api_key=(
            os.environ.get("INFERROUTE_API_KEY")
            or file_vals.get("INFERROUTE_API_KEY")
            or ""
        ),
    )


def save(api_key: str, api_url: str = DEFAULT_API_URL) -> Path:
    """Write credentials file with mode 600. Returns the path written to."""
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Ensure parent dir is restrictive too
    os.chmod(CREDS_FILE.parent, stat.S_IRWXU)
    body = (
        "# inferroute CLI credentials — created by `ir login`\n"
        f"INFERROUTE_API_URL={api_url}\n"
        f"INFERROUTE_API_KEY={api_key}\n"
    )
    CREDS_FILE.write_text(body)
    os.chmod(CREDS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return CREDS_FILE
