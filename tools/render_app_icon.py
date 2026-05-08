"""Regenerate resources/app_icon.ico from src/app_icon.py (needs Qt GUI stack)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# repo root
ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app_icon import build_app_icon  # noqa: E402


def main() -> None:
    _ = QApplication([])
    out_dir = ROOT / "resources"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "app_icon.ico"
    ok = build_app_icon().pixmap(256, 256).save(str(path), "ICO")
    if not ok:
        raise SystemExit(f"failed to write {path}")
    print(path)


if __name__ == "__main__":
    main()
