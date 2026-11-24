#!/usr/bin/env python3
"""Compare what the C++ engine and recorder/check.py report for each file of a data folder.

    python3 engine/tools/compare_check.py --data-dir DATA --replay build/release/engine/replay

The engine mirrors check.py's checks, so for every file the two must agree on when the book
was ready, when it was spoiled, the moments of missed full books, how the connection ended, the
number of problems, and every count check.py keeps (the engine keeps more). Exit code 0 means
they agree on every file; 1 lists the differences.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "recorder"))

from check import CHECKS, UNPLANNED_ENDINGS, check_file  # noqa: E402


def engine_results(replay: Path, data_dir: Path) -> dict[tuple[str, str], dict]:
    with tempfile.TemporaryDirectory() as tmp:
        files = Path(tmp) / "files.ndjson"
        run = subprocess.run(
            [str(replay), str(data_dir), "--files", str(files)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if run.returncode not in (0, 1):  # 1 only means problems were found
            sys.exit(f"the replay failed ({run.returncode}): {run.stderr}")
        lines = files.read_text().splitlines()
    return {(r["feed"], r["file"]): r for r in map(json.loads, lines)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    args = parser.parse_args()

    engine = engine_results(args.replay, args.data_dir)
    compared = 0
    differences: list[str] = []
    for feed in sorted(CHECKS):
        everything = sorted((args.data_dir / feed).glob("*/*.jsonl.gz"))
        for path in everything:
            python = check_file(feed, path, newest=(path == everything[-1]))
            cpp = engine.get((feed, path.name))
            compared += 1
            if cpp is None:
                differences.append(f"{path.name}: the engine did not report this file")
                continue
            pairs = {
                "start_ns": (python.start_ns, cpp["start_ns"]),
                "end_ns": (python.end_ns, cpp["end_ns"]),
                "ready_ns": (python.ready_ns, cpp["ready_ns"]),
                "invalid_from_ns": (python.invalid_from_ns, cpp["invalid_from_ns"]),
                "break_ns": (python.break_ns, cpp["break_ns"]),
                "ended": (python.ended, cpp["ended"]),
                "unplanned": (python.ended in UNPLANNED_ENDINGS, cpp["unplanned"]),
                "problems": (len(python.problems), len(cpp["problems"])),
            }
            for name, value in python.counts.items():
                pairs[f"counts.{name}"] = (value, cpp["counts"].get(name, 0))
            for name, (want, have) in pairs.items():
                if want != have:
                    differences.append(
                        f"{path.name}: {name}: check.py {want}, engine {have}"
                    )

    for line in differences:
        print(line)
    print(f"{compared} file(s) compared, {len(differences)} difference(s)")
    return 1 if differences or compared == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
