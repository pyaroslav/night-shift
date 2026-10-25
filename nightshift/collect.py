"""Read one database's logs for a time window and turn them into a small digest.

A target is either a Docker container (read with `docker exec`, so nothing is installed on the
database host) or plain files on this machine. Nothing here writes to the database or its logs.
"""

import glob
import shlex
import subprocess
from datetime import datetime, timedelta, timezone

from .logs import alert_entries, group, listener_entries

TAIL_BYTES = 50_000_000  # a very chatty night still fits; older lines are outside the window anyway


def _sh(container, cmd, timeout=60):
    r = subprocess.run(["docker", "exec", container, "bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def read(target: dict, path: str) -> str:
    if target.get("container"):
        rc, out, err = _sh(target["container"], f"for f in {path}; do tail -c {TAIL_BYTES} \"$f\"; done")
        if rc != 0:
            raise RuntimeError(f"{target['name']}: can't read {path}: {err.strip()[:200]}")
        return out
    return "".join(open(p, errors="replace").read()[-TAIL_BYTES:] for p in sorted(glob.glob(path)))


def alive(target: dict):
    """True/False when we can tell, None for plain files. A stopped container counts as down."""
    c = target.get("container")
    if not c:
        return None, "log files only, no liveness check"
    state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", c], capture_output=True, text=True)
    if state.stdout.strip() != "true":
        return False, f"container {c} is not running"
    # 23ai and later call it db_pmon_<SID>, older releases ora_pmon_<SID>. Oracle's images have no
    # ps or pgrep, so read /proc.
    sid = shlex.quote(target["sid"])
    rc, out, _ = _sh(c, f"for p in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $p 2>/dev/null; echo; done "
                        f"| grep -qxE '(ora|db)_pmon_{sid} *'")
    return (True, "pmon running") if rc == 0 else (False, f"no pmon process for {target['sid']}")


def digest(target: dict, since: datetime, until: datetime) -> dict:
    entries, newest, n_alert = [], None, 0
    for kind, parse in (("alert", alert_entries), ("listener", listener_entries)):
        if target.get(kind):
            every = [e for e in parse(read(target, target[kind])) if e.at <= until]
            mine = [e for e in every if since < e.at]
            if kind == "alert":
                newest, n_alert = (every[-1].at if every else None), len(mine)
            entries += mine
    entries.sort(key=lambda e: e.at)
    findings, routine = group(entries, target.get("mute", []))
    up, why = alive(target) if target.get("check_alive", True) else (None, "not checked (replay)")
    last = newest
    d = {
        "name": target["name"],
        "window": f"{since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC",
        "alive": up, "alive_note": why,
        "log_lines": len(entries), "routine": routine,
        "last_alert_log_entry": f"{last:%Y-%m-%d %H:%M UTC}" if last else "none",
        "findings": [{"severity": f.severity, "what": f.label, "count": f.count, "first": f.first, "last": f.last,
                      "codes": sorted(f.detail)[:6], "sample": f.sample, "do": f.advice} for f in findings],
    }
    if target.get("alert") and n_alert == 0 and up is not False:
        # Oracle writes to the alert log every few hours even when idle (log switches, jobs, stats).
        # A whole night of nothing usually means it isn't running, or isn't writing where we look.
        d["findings"].insert(0, {"severity": "critical", "what": "Alert log silent all night", "count": 1,
                                 "first": d["window"].split(" to ")[0] + " UTC", "last": f"{until:%Y-%m-%d %H:%M UTC}",
                                 "codes": [], "sample": f"no alert log entries in the window; last one {d['last_alert_log_entry']}",
                                 "do": "Check the instance is up and the alert log path is right."})
    if up is False:  # the most important line of the night goes first
        d["findings"].insert(0, {"severity": "critical", "what": "Database is not running now", "count": 1,
                                 "first": f"{until:%Y-%m-%d %H:%M UTC}", "last": f"{until:%Y-%m-%d %H:%M UTC}",
                                 "codes": [], "sample": why + f"; last alert log entry {d['last_alert_log_entry']}",
                                 "do": "Start it, then read the alert log from the last entries before it stopped."})
    return d


def window(hours: float = 24, until: datetime | None = None):
    until = until or datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return until - timedelta(hours=hours), until
