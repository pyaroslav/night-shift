"""Replay past nights from the lab logs: Night Shift's brief vs. pasting the raw night into the same model.

Each night is the 24 hours up to 11:00 UTC (6 AM Central). What should be in the brief is found with
plain grep on the raw log, independent of the rules: the instance died, a tablespace filled, a
deadlock, an online redo log went missing, or the alert log went silent all night. The question is
whether the two lines at the top (what a lazy person actually reads) say it, and whether they invent
numbers that aren't in the logs.

    .venv/bin/python lab/replay.py samples/ [results/replay-v1.json] > results/replay.json
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nightshift import brief, collect  # noqa: E402
from nightshift.logs import alert_entries  # noqa: E402

# what must be reported -> (grep on the raw window, words that count as saying it in the two lines)
EVENTS = {
    "instance died": (r"terminating the instance|Instance terminated", r"down|died|terminat|crash|not running|stopp|abort|shut"),
    "tablespace full": (r"ORA-0*165[0-9]|ORA-0*1688", r"tablespace|space|extend|full|1653"),
    "deadlock": (r"ORA-0*60\b", r"deadlock"),
    "redo log missing": (r"ORA-0*31[23]\b", r"redo"),
    "alert log silent": (None, r"silen|no (alert )?log|nothing logged|not running|down|no entries"),
}
RAW_PROMPT = """You are the night-shift DBA writing the morning handover for a lazy colleague.
Below is last night's Oracle alert log for database {name} (24 hours).

Write EXACTLY two lines, no title, no bullets, no markdown:
Line 1: how the night went, in under 30 words.
Line 2: the single first thing to do this morning, in under 25 words. If nothing needs doing, say they can keep sleeping.

Use only facts and numbers that appear in the log.

Log:
{log}
"""
RAW_LIMIT = 90_000  # characters, ~25K tokens, so the prompt fits a 32K context


def expected(raw: str, n_entries: int) -> set[str]:
    out = {k for k, (pat, _) in EVENTS.items() if pat and re.search(pat, raw)}
    if n_entries == 0:
        out.add("alert log silent")
    return out


def said(lines: str, events: set[str]) -> dict:
    return {e: bool(re.search(EVENTS[e][1], lines, re.I)) for e in events}


def invented(lines: str, source: str) -> list[str]:
    return sorted(brief.numbers(lines) - brief.numbers(source))


def raw_brief(name, raw):
    log = raw[-RAW_LIMIT:]
    t = time.time()
    r = brief.requests.post(f"{brief.OLLAMA}/api/chat", timeout=900, json={
        "model": brief.MODEL, "stream": False, "think": False, "options": {"temperature": 0.2, "num_ctx": 32768},
        "messages": [{"role": "user", "content": RAW_PROMPT.format(name=name, log=log)}]}).json()
    text = "\n".join(ln.strip() for ln in r["message"]["content"].splitlines() if ln.strip())
    return text, time.time() - t, r.get("prompt_eval_count"), len(raw) > RAW_LIMIT


def main(folder, reuse=None):
    """reuse: an earlier results file whose raw-log answers to keep (the raw side doesn't change between runs)."""
    folder = Path(folder)
    nights = []
    old = {(n["db"], n["night"]): n["raw"] for n in json.load(open(reuse))} if reuse else {}
    for name, alert, listener in (("lab-se2", "se2_alert.log", "se2_listener.log"), ("lab-26ai", "free_alert.log", None)):
        text = (folder / alert).read_text(errors="replace")
        es = alert_entries(text)
        day = datetime(2026, 10, 14, 11, tzinfo=timezone.utc)
        while day <= datetime(2026, 10, 26, 11, tzinfo=timezone.utc):
            since = day - timedelta(hours=24)
            mine = [e for e in es if since < e.at <= day]
            raw = "\n".join(f"{e.at:%Y-%m-%dT%H:%M:%S}\n{e.text}" for e in mine)
            target = {"name": name, "alert": str(folder / alert), "check_alive": False}
            if listener:
                target["listener"] = str(folder / listener)
            t = time.time()
            d = collect.digest(target, since, day)
            s = brief.summary([d])
            ns_s = time.time() - t
            want = expected(raw, len(mine))
            if (name, f"{day:%Y-%m-%d}") in old:
                o = old[(name, f"{day:%Y-%m-%d}")]
                rb, raw_s, raw_tokens, cut = o["lines"], o["seconds"], o["prompt_tokens"], o["truncated"]
            else:
                rb, raw_s, raw_tokens, cut = raw_brief(name, raw) if mine else ("(empty log: nothing to send)", 0, 0, False)
            src = raw + json.dumps(brief.compact([d]))
            nights.append({
                "db": name, "night": f"{day:%Y-%m-%d}", "entries": len(mine), "raw_chars": len(raw), "expected": sorted(want),
                "night_shift": {"lines": s["lines"], "by": s["by"], "tries": s["tries"], "seconds": round(ns_s, 1),
                                "prompt_chars": s["prompt_chars"], "said": said(s["lines"], want),
                                "listed": sorted(f["what"] for f in d["findings"] if f["severity"] == "critical"),
                                "invented": invented(s["lines"], src), "two_lines": len(s["lines"].splitlines()) == 2},
                "raw": {"lines": rb, "seconds": round(raw_s, 1), "prompt_tokens": raw_tokens, "truncated": cut,
                        "said": said(rb, want), "invented": invented(rb, raw) if mine else [],
                        "two_lines": len(rb.splitlines()) == 2},
            })
            print(f"{name} {day:%m-%d} entries={len(mine):5} want={sorted(want)} "
                  f"NS={nights[-1]['night_shift']['said']} raw={nights[-1]['raw']['said']}", file=sys.stderr, flush=True)
            day += timedelta(days=1)
            if name == "lab-se2" and day > datetime(2026, 10, 22, 11, tzinfo=timezone.utc):
                break  # two silent nights after it died are enough to make the point
    json.dump(nights, sys.stdout, indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples", sys.argv[2] if len(sys.argv) > 2 else None)
