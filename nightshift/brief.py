"""Turn the night's digests into the morning brief.

Gemma writes only the two-line summary at the top. The list of things that need a human is
rendered straight from the digest, so the model can't drop an outage or invent one. The two lines
are checked before they're used: every number has to be in the digest, and every database that
has a critical finding has to be named. If Gemma can't pass after a few tries, a plain template
takes its place and the brief says so.
"""

import json
import os
import re

import requests

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("NIGHTSHIFT_MODEL", "gemma4:e4b")

PROMPT = """You are the night-shift DBA writing the morning handover for a lazy colleague.
Below is a JSON digest of last night's Oracle alert and listener logs, already sorted by rules.

Write EXACTLY two lines, no title, no bullets, no markdown:
Line 1: how the night went, in under 35 words. Name each database with a critical finding and what it was,
then name the other findings briefly.
Line 2: the single first thing to do this morning, in under 25 words. If nothing needs doing, say they can keep sleeping.

Rules: use only facts and numbers that appear in the digest. Take the advice from the digest, in your own words,
without mentioning the digest or its fields. Don't count routine lines as problems.

Digest:
{digest}
"""


def compact(digests: list[dict]) -> dict:
    """What the model sees: findings without samples, which only cost tokens."""
    return {"databases": [{k: v for k, v in d.items() if k not in ("findings",)} |
                          {"findings": [{k: f[k] for k in ("severity", "what", "count", "first", "last", "do")}
                                        for f in d["findings"]]} for d in digests]}


def numbers(text: str) -> set[str]:
    return {n.lstrip("0") or "0" for n in re.findall(r"\d+", text)}


def problems(lines: str, digests: list[dict]) -> list[str]:
    out = []
    rows = [ln for ln in lines.strip().splitlines() if ln.strip()]
    if len(rows) != 2:
        out.append(f"wrote {len(rows)} lines, not 2")
    allowed = numbers(json.dumps(compact(digests)))
    bad = sorted(numbers(lines) - allowed)
    if bad:
        out.append(f"numbers not in the digest: {', '.join(bad)}")
    for d in digests:
        if any(f["severity"] == "critical" for f in d["findings"]) and d["name"].lower() not in lines.lower():
            out.append(f"didn't name {d['name']}, which has a critical finding")
    return out


def ask(prompt: str) -> str:
    r = requests.post(f"{OLLAMA}/api/chat", timeout=600, json={
        "model": MODEL, "stream": False, "think": False, "options": {"temperature": 0.2, "num_ctx": 8192},
        "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def summary(digests: list[dict], tries: int = 3) -> dict:
    prompt = PROMPT.format(digest=json.dumps(compact(digests), indent=1))
    issues = []
    for i in range(tries):
        text = ask(prompt if not issues else prompt + "\nYour last answer had problems: " + "; ".join(issues) +
                   ". Write the two lines again.")
        text = "\n".join(ln.strip().strip("*").strip() for ln in text.splitlines() if ln.strip())
        issues = problems(text, digests)
        if not issues:
            return {"lines": text, "by": MODEL, "tries": i + 1, "prompt_chars": len(prompt)}
    return {"lines": template(digests), "by": "template", "tries": tries, "prompt_chars": len(prompt),
            "rejected": issues}


def template(digests: list[dict]) -> str:
    crit = [d["name"] for d in digests if any(f["severity"] == "critical" for f in d["findings"])]
    if not crit:
        return "Quiet night: nothing critical on any database.\nNothing needs you first thing; keep sleeping."
    return f"Critical findings on {', '.join(crit)}.\nStart with {crit[0]}: see the first item below."


def render(digests: list[dict], s: dict, took: str = "") -> str:
    """The whole brief as Markdown."""
    out = ["# Night Shift", "", *[f"> {ln}" for ln in s["lines"].splitlines()], ""]
    if s["by"] == "template":
        out += [f"_({MODEL} couldn't write a faithful summary: {'; '.join(s.get('rejected', []))}. Template used.)_", ""]
    for d in digests:
        up = {True: "up", False: "**DOWN**", None: "not checked"}[d["alive"]]
        out += [f"## {d['name']}: {up}", f"{d['window']} · {d['log_lines']} log lines, {d['routine']} routine"]
        real = [f for f in d["findings"] if f["severity"] in ("critical", "attention")]
        if not real:
            out += ["", "Nothing to look at.", ""]
            continue
        out.append("")
        for f in real:
            mark = "🔴" if f["severity"] == "critical" else "🟡"
            when = f["first"] if f["count"] == 1 else f"{f['count']}× {f['first']} → {f['last']}"
            codes = f" ({', '.join(f['codes'])})" if f["codes"] else ""
            dl = f.get("deadlock")
            if dl:  # read from the trace file: what actually deadlocked, instead of "read the trace"
                out.append(f"- {mark} **{f['what']}**{codes}, {when}. {dl['kind'][0].upper() + dl['kind'][1:]}. {dl['fix']}")
                out += [f"  - session {s['sid']} ({s['user']}): `{s['sql']}`" for s in dl["sessions"]]
            else:
                out += [f"- {mark} **{f['what']}**{codes}, {when}. {f['do']}", f"  `{f['sample'][:160]}`"]
        out.append("")
    if took:
        out += [f"_{took}_"]
    return "\n".join(out) + "\n"
