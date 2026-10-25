#!/usr/bin/env python3
"""Copy core/paperkb_core into a skill directory (or an external vendor dir).

Skills are installed by copying their directory, so they cannot import code
from outside it.  This script vendors a pinned copy and writes VENDORED.json
(version + per-file sha256) so drift is detectable:

    python tools/sync_core.py skills/paper-knowledge-online/scripts
    python tools/sync_core.py /path/to/paper-knowledge-local/vendor --check
"""
import argparse, hashlib, json, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "core" / "paperkb_core"

def manifest(d: Path) -> dict:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*.py"))}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dest", type=Path, help="directory that will contain paperkb_core/")
    ap.add_argument("--check", action="store_true", help="only verify the vendored copy matches")
    a = ap.parse_args()
    dst = a.dest / "paperkb_core"
    want = manifest(SRC)
    if a.check:
        have = manifest(dst) if dst.exists() else {}
        bad = sorted(set(want) ^ set(have) | {k for k in want if have.get(k) != want[k]})
        print("in sync" if not bad else "OUT OF SYNC: " + ", ".join(bad))
        return 0 if not bad else 1
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    sys.path.insert(0, str(ROOT / "core"))
    import paperkb_core
    (a.dest / "VENDORED.json").write_text(json.dumps(
        {"package": "paperkb_core", "version": paperkb_core.__version__, "files": want}, indent=1) + "\n")
    print(f"vendored paperkb_core {paperkb_core.__version__} -> {dst} ({len(want)} files)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
