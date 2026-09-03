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
import hashlib
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
    "SECURITY.md",
    "scripts/rc.py",
    "scripts/journey_check.py",
    "scripts/install.py",
    "references/environment.md",
    "references/troubleshooting.md",
    "references/user-journey.md",
)

# Development and verification tooling. It lives in the repo (and in the locally
# installed copy, where the self-checks run) but is deliberately NOT published:
# journey_check.py asserts that rc.py contains no private-key access, so its own
# source necessarily carries those very patterns and would trip a static scan.
DEV_ONLY = ("scripts/journey_check.py",)


def sync_dist() -> None:
    shipped = [rel for rel in TRACKED if rel not in DEV_ONLY]
    for rel in shipped:
        src = SKILL_DIR / rel
        dst = DIST_DIR / rel
        if not src.exists():
            sys.exit(f"source missing: {rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # Prune anything the published set no longer includes, so a file that stops
    # being shipped cannot linger in dist/ and reach the SkillHub scan.
    keep = {(DIST_DIR / rel) for rel in shipped}
    for path in sorted(DIST_DIR.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path not in keep:
            path.unlink()
    for cache in DIST_DIR.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_dist() -> int:
    """Every published file matches its source, and nothing extra is published.

    This is the authoritative source <-> dist check, because it knows which
    files are dev-only. Comparing with install.py --dest would demand files that
    are deliberately withheld from the package.
    """
    shipped = [rel for rel in TRACKED if rel not in DEV_ONLY]
    problems = []
    for rel in shipped:
        src, dst = SKILL_DIR / rel, DIST_DIR / rel
        if not dst.exists():
            problems.append(f"{rel} (absent from dist)")
        elif sha256(src) != sha256(dst):
            problems.append(f"{rel} (differs)")
    keep = {(DIST_DIR / rel) for rel in shipped}
    for path in sorted(DIST_DIR.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path not in keep:
            problems.append(f"{path.relative_to(DIST_DIR)} (must not be published)")
    if problems:
        print("DIST DRIFT")
        for item in problems:
            print(f"  - {item}")
        return 1
    print(f"DIST OK: {len(shipped)} published files in sync")
    return 0


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
        return verify_dist() or verify_zip()
    sync_dist()
    build_zip()
    return verify_dist() or verify_zip()


if __name__ == "__main__":
    sys.exit(main())
