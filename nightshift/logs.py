"""Split Oracle alert and listener logs into timestamped entries, and sort each entry into a finding.

The rules are deliberately plain: a DBA should be able to read this file and agree with every line.
Anything that matches nothing but still carries an ORA- code becomes an "other error", so new
problems are never silently dropped.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?([+-]\d\d:\d\d|Z)?\s*$")
OLD = re.compile(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\w{3} +\d+ \d\d:\d\d:\d\d \d{4})\s*$")  # 11g alert log
LSNR = re.compile(r"^(\d\d-[A-Z]{3}-\d{4} \d\d:\d\d:\d\d)(?::\d+)? \* ?(.*)$")  # listener.log line


def _iso(s, tz):
    t = datetime.fromisoformat(s + (tz or "+00:00").replace("Z", "+00:00"))
    return t.astimezone(timezone.utc)


@dataclass
class Entry:
    at: datetime
    text: str


def alert_entries(text: str) -> list[Entry]:
    """Alert log: a timestamp line, then the message lines until the next timestamp."""
    out, at, buf = [], None, []
    for line in text.splitlines():
        m, o = ISO.match(line), OLD.match(line)
        if m or o:
            if at and buf:
                out.append(Entry(at, "\n".join(buf)))
            at = _iso(*m.groups()) if m else datetime.strptime(" ".join(o.group(1).split()), "%b %d %H:%M:%S %Y").replace(
                tzinfo=timezone.utc)
            buf = []
        elif at and line.strip():
            buf.append(line.rstrip())
    if at and buf:
        out.append(Entry(at, "\n".join(buf)))
    return out


def listener_entries(text: str) -> list[Entry]:
    """listener.log: one event per line, with either the classic or the ISO timestamp."""
    out, at = [], None
    for line in text.splitlines():
        m = LSNR.match(line)
        if m:
            out.append(Entry(datetime.strptime(m.group(1), "%d-%b-%Y %H:%M:%S").replace(tzinfo=timezone.utc), line))
            continue
        i = ISO.match(line)
        if i:
            at = _iso(*i.groups())
        elif at and line.strip():
            out.append(Entry(at, line.rstrip()))
    return out


# (key, severity, label, what to do, patterns). First match wins, so the specific rules come first.
# severity: "critical" = look now; "attention" = look today; "noise" = counted, never reported.
RULES = [
    ("instance_down", "critical", "Instance terminated", "Find why it died (the line before it), start it, check it's open.",
     [r"terminating the instance", r"Instance terminated by", r"abnormal instance termination"]),
    ("process_died", "critical", "Critical background process died", "Check the host for memory pressure (OOM killer) around that time.",
     [r"Instance Critical Process .* died"]),
    ("internal_error", "critical", "Internal error (ORA-600/7445)", "Look up the first argument on My Oracle Support; keep the trace file.",
     [r"ORA-0*600\b", r"ORA-0*7445\b"]),
    ("corruption", "critical", "Block corruption", "Run RMAN VALIDATE on the datafile named in the message.",
     [r"ORA-0*1578\b", r"[Cc]orrupt block"]),
    ("archiver_stuck", "critical", "Archiver stuck", "Free space in the archive destination or FRA before the database hangs.",
     [r"ORA-0*257\b", r"ORA-0*1601[46]\b", r"ORA-0*19809\b", r"ORA-0*19815\b", r"[Aa]rchiver error"]),
    ("space", "critical", "Out of space in a tablespace", "Add or extend a datafile, or find what grew.",
     [r"ORA-0*165[0-9]\b", r"ORA-0*1688\b", r"ORA-0*1691\b", r"ORA-0*1652\b", r"unable to extend"]),
    ("redo_missing", "critical", "Online redo log file missing", "Check the file and its member status in V$LOGFILE.",
     [r"ORA-0*312\b", r"ORA-0*313\b"]),
    ("memory", "critical", "Out of shared memory", "Check SGA sizing and what filled the shared pool.",
     [r"ORA-0*4031\b"]),
    ("processes", "critical", "Ran out of processes or sessions", "Find the connection leak; raise PROCESSES only after.",
     [r"ORA-0*20\b", r"ORA-0*18\b"]),
    ("listener_lost_db", "critical", "Listener lost the database service", "Check the instance is up and registered.",
     [r"\* service_died \*"]),
    ("deadlock", "attention", "Deadlocks", "Read the trace file: it names both sessions and the SQL.",
     [r"ORA-0*60\b", r"Deadlock detected"]),
    ("checkpoint", "attention", "Checkpoint not complete", "Redo logs are too small or too few for the load.",
     [r"Checkpoint not complete"]),
    ("snapshot_too_old", "attention", "Snapshot too old (ORA-1555)", "Check undo retention against the longest query.",
     [r"ORA-0*1555\b"]),
    ("failed_job", "attention", "Scheduler job failed", "Run the job by hand to see the full error.",
     [r"ORA-0*12012\b"]),
    ("connect_timeout", "attention", "Inbound connection timeouts", "Look for a client or network problem; check the source address.",
     [r"ORA-0*3136\b", r"TNS-0*12535\b", r"TNS-0*12170\b", r"ORA-0*12170\b"]),
    ("login_failed", "attention", "Failed logins", "Check whether it's a wrong password in a job or someone guessing.",
     [r"ORA-0*1017\b", r"ORA-0*28000\b"]),
    ("swapping", "attention", "Host swapping", "The host is short of memory.", [r"Heavy swapping"]),
    ("noise", "noise", "Routine", "",
     [r"advanced to log sequence", r"Current log# \d", r"Resize operation completed", r"scheduler window",
      r"Resource Manager plan", r"ADDED INTERVAL PARTITION", r"service_update", r"Archived Log entry", r"^Completed:",
      r"Starting background process", r"establish \*", r"\* ping \*", r"\* status \*", r"Thread \d+ cannot allocate"]),
]
COMPILED = [(k, s, lab, adv, [re.compile(p, re.M) for p in pats]) for k, s, lab, adv, pats in RULES]
ORA = re.compile(r"\b(ORA|TNS)-0*(\d+)")
JOB = re.compile(r'job ("[^"]+"(?:\."[^"]+")?)')  # job "SYS"."ORA$AT_OS_OPT_SY_780"


def classify(e: Entry):
    for key, sev, label, advice, pats in COMPILED:
        if any(p.search(e.text) for p in pats):
            return key, sev, label, advice
    if ORA.search(e.text):
        return "other_error", "attention", "Other Oracle error", "Read the message; it isn't one of the known patterns."
    return "info", "noise", "Informational", ""


@dataclass
class Finding:
    key: str
    severity: str
    label: str
    advice: str
    count: int = 0
    first: str = ""
    last: str = ""
    sample: str = ""
    detail: set = field(default_factory=set)  # job names, error codes: what tells two findings apart


# One outage writes several lines (state dump, "terminated by PMON", listener losing the service).
# For these, lines less than 10 minutes apart are one incident.
INCIDENTS = {"instance_down", "process_died", "redo_missing", "listener_lost_db", "archiver_stuck"}


def group(entries: list[Entry], mute: list[str] = ()) -> tuple[list[Finding], int]:
    """Group entries into findings. Returns the findings (critical first) and how many lines were routine.

    `mute` is a list of regexes for things you've decided to live with (a known bug, a job you'll fix
    next month); matching lines count as routine.
    """
    found: dict[tuple, Finding] = {}
    noise = 0
    muted = [re.compile(m) for m in mute]
    last_at: dict[tuple, datetime] = {}
    for e in entries:
        key, sev, label, advice = classify(e)
        if sev == "noise" or any(m.search(e.text) for m in muted):
            noise += 1
            continue
        codes = sorted({f"{a}-{int(b):05d}" for a, b in ORA.findall(e.text)})
        sub = ""
        if key == "failed_job":  # a job that fails every 10 minutes is one finding, named after the job
            m = JOB.search(e.text)
            sub = re.sub(r"_\d+$", "_*", m.group(1).replace('"', "")) if m else ""
        elif key == "other_error":
            sub = codes[0] if codes else ""
        f = found.setdefault((key, sub), Finding(key, sev, label, advice))
        if key not in INCIDENTS or (e.at - last_at.get((key, sub), e.at - timedelta(days=1))) > timedelta(minutes=10):
            f.count += 1
        last_at[(key, sub)] = e.at
        stamp = e.at.strftime("%Y-%m-%d %H:%M UTC")
        f.first = f.first or stamp
        f.last = stamp
        if not f.sample:  # the lines that made the match, not whatever else shares the timestamp
            pats = [p for k, sev, *_, ps in COMPILED if sev != "noise" for p in ps]  # e.g. keep "MMAN died" with the outage
            lines = e.text.splitlines()
            hit = [ln for ln in lines if any(p.search(ln) for p in pats) or ORA.search(ln)]
            f.sample = " | ".join((hit or lines)[:3])[:240]
        if sub:
            f.detail.add(sub)
        f.detail.update(c for c in codes if c not in ("ORA-06512",))
    order = {"critical": 0, "attention": 1}
    return sorted(found.values(), key=lambda f: (order[f.severity], f.first)), noise
