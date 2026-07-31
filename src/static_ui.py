"""Resolve built React UI (web/dist) for FastAPI static serving."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_web_dist() -> Path | None:
    """
    Find Vite build output.

    Order:
    1. ANTIDETECT_WEB_DIST
    2. src/web_dist (bundled next to api_server — what git ships)
    3. <repo>/web/dist (local ``npm run build`` without copying)
    4. PyInstaller _MEIPASS/web_dist when frozen
    """
    env = (os.environ.get("ANTIDETECT_WEB_DIST") or "").strip()
    if env:
        p = Path(env).expanduser()
        if (p / "index.html").is_file():
            return p.resolve()
        if p.is_file() and p.name == "index.html":
            return p.parent.resolve()

    here = Path(__file__).resolve().parent

    bundled = here / "web_dist"
    if (bundled / "index.html").is_file():
        return bundled.resolve()

    # .../src -> repo/web/dist
    repo_dist = here.parent / "web" / "dist"
    if (repo_dist / "index.html").is_file():
        return repo_dist.resolve()

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            frozen = Path(meipass) / "web_dist"
            if (frozen / "index.html").is_file():
                return frozen.resolve()

    return None
