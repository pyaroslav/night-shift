"""Read a deadlock trace file so the brief can say *what* deadlocked, not just "read the trace".

Only the deadlock graph and each waiting session's user and current SQL are kept. Client machine,
OS user and program names stay out of the brief.
"""

import re

GRAPH_ROW = re.compile(r"^(TX|TM|UL|[A-Z]{2})-[0-9A-F-]+\s+\d+\s+(\d+)\s+(\w+)?\s+(\w+)?\s+\d+\s+\d+\s+(\d+)\s+(\w+)?\s+(\w+)?\s+\d+",
                       re.M)
SESSION = re.compile(r"^Session (\d+):\n(?:.*\n)*?\s+sid: \d+ ser: \d+ audsid: \d+ user: \d+/(\S+)\n(?:.*\n)*?\s+current SQL[^\n]*\n\s+(.+)",
                     re.M)


def deadlock(text: str) -> dict | None:
    """The kind of deadlock and the sessions in it, from the top of an ORA-00060 trace."""
    if "DEADLOCK DETECTED" not in text:
        return None
    rows = GRAPH_ROW.findall(text)
    sessions = [{"sid": int(s), "user": u, "sql": " ".join(q.split())[:160]} for s, u, q in SESSION.findall(text)]
    types = sorted({r[0] for r in rows})
    modes = {(r[2] or r[3], r[5] or r[6]) for r in rows}  # (blocker holds, waiter waits)
    if types == ["TX"] and all(m == ("X", "X") for m in modes):
        kind = "row lock deadlock: two sessions updated the same rows in opposite order"
        fix = "Make the application touch rows in one order (e.g. by key), or commit sooner."
    elif "TM" in types:
        kind = "table lock (TM) deadlock: often a foreign key without an index"
        fix = "Index the foreign key columns on the child table named in the trace."
    elif types == ["TX"] and any("S" in m for m in modes):
        kind = "TX deadlock waiting in share mode: often a unique/primary key insert or an ITL or bitmap index"
        fix = "Look at the waiting SQL: duplicate key inserts, bitmap indexes on OLTP tables, or INITRANS."
    else:
        kind = f"{'/'.join(types) or 'unknown'} deadlock"
        fix = "Read the deadlock graph in the trace."
    return {"kind": kind, "fix": fix, "sessions": sessions[:4]}


TRACE_PATH = re.compile(r"(?:More info in file|Errors in file) (/\S+?\.trc)")
