#!/usr/bin/env python3
"""Build the installable skill directory and release ZIP from source.

Usage:
    python scripts/build_dist.py            # sync dist/ and write dist/radeon-cloud-connector.zip
    python scripts/build_dist.py --check    # verify the ZIP matches dist/ without writing

The source tree is the single source of truth. This script copies the tracked
skill files into ``dist/radeon-cloud-connector`` and packages that directory as a
ZIP with the skill root prefix ``radeon-cloud-connector/``. The ZIP is git-ignored
(generated artifact); ``dist/radeon-cloud-connector`` is committed so the skill is
installable directly from a clone.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
DIST_DIR = SKILL_DIR / "dist" / "radeon-cloud-connector"
ZIP_PATH = SKILL_DIR / "dist" / "radeon-cloud-connector.zip"

TRACKED = (
    "SKILL.md",
    "config.yaml",
    "scripts/rc.py",
    "scripts/journey_check.py",
    "scripts/install.py",
    "references/environment.md",
    "references/troubleshooting.md",
    "references/user-journey.md",
)


def sync_dist() -> None:
    for rel in TRACKED:
        src = SKILL_DIR / rel
        dst = DIST_DIR / rel
        if not src.exists():
            sys.exit(f"source missing: {rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for cache in DIST_DIR.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def build_zip() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(DIST_DIR.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                rel = "radeon-cloud-connector/" + str(p.relative_to(DIST_DIR)).replace("\\", "/")
                z.write(p, rel)


def verify_zip() -> int:
    with zipfile.ZipFile(ZIP_PATH) as z:
        if z.testzip() is not None:
            print("ZIP has a corrupt member")
            return 1
        znames = set(z.namelist())
    expected = set()
    for p in DIST_DIR.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            expected.add("radeon-cloud-connector/" + str(p.relative_to(DIST_DIR)).replace("\\", "/"))
    if znames != expected:
        print("ZIP set mismatch")
        print("missing:", sorted(expected - znames))
        print("extra:", sorted(znames - expected))
        return 1
    print(f"ZIP OK: {len(expected)} files, {ZIP_PATH.stat().st_size} bytes")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify the ZIP matches dist/, do not write")
    args = ap.parse_args()
    if args.check:
        return verify_zip()
    sync_dist()
    build_zip()
    return verify_zip()


if __name__ == "__main__":
    sys.exit(main())
