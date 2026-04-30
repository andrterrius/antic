"""Regenerate src/chromium_release_versions.py from googlesource +refs (tags)."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "src" / "chromium_release_versions.py"
REFS_URL = "https://chromium.googlesource.com/chromium/src/+refs"


def load_html(source: str | None) -> str:
    if source:
        return Path(source).read_text(encoding="utf-8", errors="replace")
    req = Request(REFS_URL, headers={"User-Agent": "antidetect-version-sync/1.0"})
    with urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", errors="replace")


def emit(versions: list[str]) -> None:
    lines = [
        '"""Chromium release tags (MAJOR.MINOR.BUILD.PATCH) from googlesource +refs.',
        "",
        f"Snapshot source: {REFS_URL}",
        "Regenerate: python tools/emit_chromium_versions_module.py",
        '"""',
        "",
        "CHROMIUM_RELEASE_VERSIONS: tuple[str, ...] = (",
    ]
    for v in versions:
        lines.append(f'    "{v}",')
    lines.append(")")
    lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "html_file",
        nargs="?",
        help="Optional path to saved +refs HTML; default: fetch from googlesource",
    )
    args = p.parse_args()
    text = load_html(args.html_file)
    pat = re.compile(r"refs/tags/(\d+\.\d+\.\d+\.\d+)")
    versions = sorted(
        set(pat.findall(text)),
        key=lambda s: tuple(map(int, s.split("."))),
        reverse=True,
    )
    if not versions:
        print("No X.Y.Z.W tags found.", file=sys.stderr)
        return 1
    emit(versions)
    print(len(versions), "versions ->", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
