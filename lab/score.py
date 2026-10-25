"""Score a replay results file (the answers are stored, so changing the scoring needs no model runs).

    .venv/bin/python lab/score.py results/replay.json
"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay import said  # noqa: E402

nights = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "results/replay.json"))
logged = [n for n in nights if n["entries"]]
events = [(n, e) for n in nights for e in n["expected"]]
print(f"{len(nights)} nights, {len(logged)} with log lines, {len(events)} must-report events")
for side, name in (("night_shift", "Night Shift"), ("raw", "raw log to the same model")):
    hit = sum(said(n[side]["lines"], {e})[e] for n, e in events)
    two = sum(len(n[side]["lines"].splitlines()) == 2 for n in logged)
    unver = [n["night"] + " " + n["db"] + ": " + ",".join(n[side]["invented"]) for n in nights if n[side]["invented"]]
    secs = [n[side]["seconds"] for n in logged]
    print(f"\n{name}: events in the two lines {hit}/{len(events)}; kept to two lines {two}/{len(logged)}; "
          f"median {statistics.median(secs):.1f} s, max {max(secs):.1f} s")
    for n, e in events:
        if not said(n[side]["lines"], {e})[e]:
            print(f"  missed {e} on {n['db']} {n['night']}")
    for u in unver:
        print(f"  numbers not in the log: {u}")
raw_cut = [n for n in nights if n["raw"]["truncated"]]
print(f"\nraw nights over the {90_000:,}-character limit (start of the night cut off): {len(raw_cut)}; "
      f"largest night {max(n['raw_chars'] for n in nights):,} characters; "
      f"Night Shift prompt median {statistics.median(n['night_shift']['prompt_chars'] for n in nights):,.0f} characters")
