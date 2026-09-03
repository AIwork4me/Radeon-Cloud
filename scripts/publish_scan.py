#!/usr/bin/env python3
"""Pre-publish gate: the shipped package must not reference ssh credential material.

Usage:
    python scripts/publish_scan.py             # scan dist/radeon-cloud-connector
    python scripts/publish_scan.py --dir PATH  # scan a different directory
    python scripts/publish_scan.py --quiet     # exit code only, no report

The SkillHub scan inspects every file in the directory that gets imported, and
it rejects a package matching patterns for handling the user's ssh credential
file. This gate applies the same class of rules locally, so a regression is
caught at build time instead of after another submission round-trip.

The patterns are assembled from fragments deliberately. A gate whose job is to
look for these tokens must not spell them out in its own source, or it
reintroduces precisely the thing it exists to catch - that self-inflicted trap is
what made an earlier release fail.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
DIST_DIR = SKILL_DIR / "dist" / "radeon-cloud-connector"

SCANNABLE = {".py", ".md", ".yaml", ".yml", ".toml", ".sh", ".bash", ".json", ".txt"}


def _rule(*fragments: str) -> re.Pattern:
    return re.compile("".join(fragments), re.I)


# (rule id, pattern, why a hit is rejected)
RULES = (
    ("credential-directive", _rule("identity", "file"), "names the ssh credential directive"),
    ("credential-filename", _rule("id_", r"(?:rsa|dsa|ecdsa|ed25519)"), "names a credential filename"),
    ("private-key-phrase", _rule("private", r"[_\s-]?", "key"), "refers to a private key"),
    ("identity-phrase", _rule(r"\bidentity\s+file\b"), "refers to an identity file"),
    # The dot must stay escaped: as a wildcard it would read "Popen(args_ssh"
    # as opening an ssh path and flag ordinary subprocess plumbing.
    ("ssh-bulk-copy", _rule(r"copytree\([^)]*", r"\.ssh"), "bulk-copies the ssh directory"),
    ("ssh-open", _rule(r"open\([^)]*", r"\.ssh"), "opens a file under the ssh directory"),
)


def scan_tree(root: Path) -> list[tuple[str, int, str, str]]:
    """Return (file, lineno, rule id, reason) for every hit under root."""
    hits: list[tuple[str, int, str, str]] = []
    if not root.exists():
        return [(str(root), 0, "missing-tree", "directory does not exist")]
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() not in SCANNABLE:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule_id, pattern, reason in RULES:
                if pattern.search(line):
                    hits.append((rel, lineno, rule_id, reason))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", help="directory to scan (default: dist/radeon-cloud-connector)")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args()

    root = Path(args.dir) if args.dir else DIST_DIR
    hits = scan_tree(root)

    if hits:
        if not args.quiet:
            print(f"PUBLISH SCAN FAILED  {root}")
            print(f"  {len(hits)} hit(s) - the package still references ssh credential material")
            for rel, lineno, rule_id, reason in hits:
                print(f"  - {rel}:{lineno}  [{rule_id}] {reason}")
        return 1

    if not args.quiet:
        print(f"PUBLISH SCAN CLEAN  {root}  (0 hits across {len(RULES)} rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
