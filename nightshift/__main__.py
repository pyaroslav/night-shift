"""night-shift: read your Oracle logs overnight, get a two-line brief in the morning.

    night-shift server                  # local Temporal (one binary, SQLite file, UI on :8233)
    night-shift worker                  # runs the steps; leave it running
    night-shift schedule --at 06:00     # every morning, local time
    night-shift run                     # one night now, through Temporal
    night-shift now                     # same, without Temporal (quick look)
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")


async def client():
    from temporalio.client import Client
    return await Client.connect(ADDRESS)


def server(a):
    exe = a.temporal or next(iter(sorted((ROOT / ".temporal-bin").glob("temporal*"))), None) or "temporal"
    (ROOT / ".temporal").mkdir(exist_ok=True)
    os.execvp(str(exe), [str(exe), "server", "start-dev", "--db-filename", str(ROOT / ".temporal" / "night-shift.db"),
                         "--ui-port", "8233", "--port", "7233", "--log-level", "warn"])


async def worker(a):
    from temporalio.worker import Worker
    from .flow import QUEUE, NightShift, deliver, read_logs, write_summary
    w = Worker(await client(), task_queue=QUEUE, workflows=[NightShift], activities=[read_logs, write_summary, deliver],
               activity_executor=ThreadPoolExecutor(8), identity=f"night-shift-worker-{os.getpid()}")
    print(f"worker on {ADDRESS}, queue {QUEUE}", flush=True)
    await w.run()


async def run(a):
    from .flow import QUEUE, NightIn, NightShift, load_targets
    c = await client()
    wid = f"night-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    path = await c.execute_workflow(NightShift.run, NightIn(load_targets(a.targets), a.hours, a.until or "", not a.quiet),
                                    id=wid, task_queue=QUEUE)
    print((ROOT / path).read_text() if not Path(path).is_absolute() else Path(path).read_text())
    print(f"saved {path}", file=sys.stderr)


async def schedule(a):
    from temporalio.client import Schedule, ScheduleActionStartWorkflow, ScheduleSpec, ScheduleCalendarSpec, ScheduleRange
    from .flow import QUEUE, NightIn, NightShift, load_targets
    c = await client()
    hh, mm = (int(x) for x in a.at.split(":"))
    spec = ScheduleSpec(calendars=[ScheduleCalendarSpec(hour=[ScheduleRange(hh)], minute=[ScheduleRange(mm)])],
                        time_zone_name=a.tz)
    try:
        await c.get_schedule_handle("night-shift-morning").delete()
    except Exception:
        pass
    await c.create_schedule("night-shift-morning", Schedule(
        action=ScheduleActionStartWorkflow(NightShift.run, NightIn(load_targets(a.targets), a.hours), id="night-shift-scheduled",
                                           task_queue=QUEUE), spec=spec))
    print(f"brief every day at {a.at} {a.tz}; see http://localhost:8233/namespaces/default/schedules")


def now(a):
    from . import brief, collect
    from .flow import load_targets
    t0 = time.time()
    since, until = collect.window(a.hours, datetime.fromisoformat(a.until) if a.until else None)
    ds = [collect.digest(t, since, until) for t in load_targets(a.targets)]
    s = brief.summary(ds)
    if a.json:
        print(json.dumps({"digests": ds, "summary": s}, indent=1))
    else:
        print(brief.render(ds, s, f"{time.time() - t0:.1f} s · summary by {s['by']} ({s['tries']} tries)"))


def main():
    ap = argparse.ArgumentParser(prog="night-shift", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("server"); p.add_argument("--temporal", help="path to the temporal CLI")
    sub.add_parser("worker")
    for name in ("run", "now", "schedule"):
        p = sub.add_parser(name)
        p.add_argument("--targets", default=str(ROOT / "targets.json"))
        p.add_argument("--hours", type=float, default=24)
        if name == "schedule":
            p.add_argument("--at", default="06:00")
            p.add_argument("--tz", default=os.environ.get("TZ", "America/Chicago"))
        else:
            p.add_argument("--until", help="end of the window, ISO time (to replay an old night)")
        if name == "run":
            p.add_argument("--quiet", action="store_true", help="no desktop notification")
        if name == "now":
            p.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.cmd == "server":
        return server(a)
    if a.cmd == "now":
        return now(a)
    asyncio.run({"worker": worker, "run": run, "schedule": schedule}[a.cmd](a))


if __name__ == "__main__":
    main()
