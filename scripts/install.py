#!/usr/bin/env python3
"""Install (or re-sync) the radeon-cloud skill for the current user.

    python scripts/install.py            # sync to ~/.workbuddy-ai/skills/<name>
    python scripts/install.py --check    # report drift without writing
    python scripts/install.py --dest DIR # install somewhere else

The skill directory name comes from the `name:` field in SKILL.md, not from the
development folder, which is human-readable ("Radeon Cloud Connector").
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent

# Shipped with the published package; these must always be present.
TRACKED = ("SKILL.md", "config.yaml", "SECURITY.md", "scripts/rc.py", "scripts/install.py",
           "references/environment.md", "references/troubleshooting.md")

# Development and verification tooling, matched by glob rather than by name. It
# lives in a repo checkout and in a locally installed copy, where the self-checks
# run, but is deliberately not published - so sync it only when the source tree
# actually has it. Requiring it unconditionally made install.py exit on the
# published package, which by design does not carry these files; and naming it
# literally would leave the package pointing at files it does not ship, which a
# reviewer reads as an evidence gap.
DEV_GLOBS = ("scripts/*_check.py", "references/user-journey.md")


def dev_files() -> list[str]:
    """Verification tooling this source tree happens to carry, if any."""
    return [
        str(p.relative_to(SKILL_DIR)).replace("\\", "/")
        for pattern in DEV_GLOBS
        for p in sorted(SKILL_DIR.glob(pattern))
    ]


def skill_name() -> str:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"^name:\s*(.+)$", text, re.M)
    if not match:
        sys.exit("SKILL.md has no `name:` frontmatter field")
    return match.group(1).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report drift, change nothing")
    ap.add_argument("--dest", help="install directory (default: ~/.workbuddy-ai/skills/<name>)")
    args = ap.parse_args()

    name = skill_name()
    dest = Path(args.dest) if args.dest else Path.home() / ".workbuddy-ai" / "skills" / name

    missing_src = [r for r in TRACKED if not (SKILL_DIR / r).exists()]
    if missing_src:
        sys.exit(f"source files missing: {missing_src}")

    files = list(TRACKED) + dev_files()

    drift = []
    for rel in files:
        src, dst = SKILL_DIR / rel, dest / rel
        if not dst.exists():
            drift.append(f"{rel} (absent)")
        elif sha256(src) != sha256(dst):
            drift.append(f"{rel} (differs)")

    if args.check:
        if drift:
            print(f"DRIFT  {dest}")
            for item in drift:
                print(f"  - {item}")
            return 1
        print(f"IN SYNC  {dest}")
        return 0

    for rel in files:
        src, dst = SKILL_DIR / rel, dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  {rel}")

    # Byte-code caches must never ship: they pin an interpreter version and
    # make the installed copy look larger than it is.
    for cache in dest.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
        print(f"  removed {cache.relative_to(dest)}")

    print()
    print(f"installed: {dest}")
    checkers = [f for f in files if f.startswith("scripts/") and f.endswith("_check.py")]
    if checkers:
        print(f"verify with:  python {checkers[0]} --phase review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
