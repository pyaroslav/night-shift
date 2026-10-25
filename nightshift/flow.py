"""The Temporal workflow: read every database's logs, write the brief, deliver it.

Why Temporal: a brief that silently doesn't arrive is worse than no tool at all, because you stop
checking yourself. So every step retries on its own schedule, a database that can't be read
becomes a finding instead of a crash, and if the laptop sleeps or the worker dies halfway through,
the run picks up where it stopped instead of starting over.
"""

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from . import brief, collect

QUEUE = "night-shift"
BRIEFS = os.environ.get("NIGHTSHIFT_BRIEFS", "briefs")  # resolved in the activity, not in workflow code


@dataclass
class NightIn:
    targets: list[dict]
    hours: float = 24
    until: str = ""  # ISO time; empty = now. Set it to replay an old night.
    notify: bool = True


@dataclass
class CollectIn:
    target: dict
    since: str
    until: str


@dataclass
class DeliverIn:
    markdown: str
    until: str
    headline: str
    notify: bool


@activity.defn
def read_logs(a: CollectIn) -> dict:
    activity.logger.info("reading %s", a.target["name"])
    return collect.digest(a.target, datetime.fromisoformat(a.since), datetime.fromisoformat(a.until))


@activity.defn
def write_summary(digests: list[dict]) -> dict:
    return brief.summary(digests)


@activity.defn
def deliver(a: DeliverIn) -> str:
    out = Path(BRIEFS).resolve()
    out.mkdir(exist_ok=True)
    path = out / f"{datetime.fromisoformat(a.until):%Y-%m-%d_%H%M}.md"
    path.write_text(a.markdown)
    (out / "latest.md").write_text(a.markdown)
    if a.notify:
        subprocess.run(["notify-send", "-a", "Night Shift", "Night Shift", a.headline], check=False, capture_output=True)
    return f"{Path(BRIEFS).name}/{path.name}"


def unreadable(target: dict, since: datetime, until: datetime, err: str) -> dict:
    """A database whose logs couldn't be read still gets a line in the brief: silence isn't 'all quiet'."""
    return {"name": target["name"], "window": f"{since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC",
            "alive": None, "alive_note": "logs unreadable", "log_lines": 0, "routine": 0, "last_alert_log_entry": "unknown",
            "findings": [{"severity": "critical", "what": "Couldn't read the logs", "count": 1,
                          "first": f"{until:%Y-%m-%d %H:%M UTC}", "last": f"{until:%Y-%m-%d %H:%M UTC}", "codes": [],
                          "sample": err[:200], "do": "Check the host or container is reachable; the night is unknown."}]}


@workflow.defn
class NightShift:
    @workflow.run
    async def run(self, n: NightIn) -> str:
        until = datetime.fromisoformat(n.until) if n.until else workflow.now().astimezone(timezone.utc).replace(
            second=0, microsecond=0)
        since = until - timedelta(hours=n.hours)
        started = workflow.now()

        async def one(t):
            try:
                return await workflow.execute_activity(
                    read_logs, CollectIn(t, since.isoformat(), until.isoformat()),
                    start_to_close_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=15), backoff_coefficient=2,
                                             maximum_attempts=4))
            except ActivityError as e:
                return unreadable(t, since, until, str(e.cause or e))

        digests = list(await asyncio.gather(*(one(t) for t in n.targets)))
        try:
            s = await workflow.execute_activity(
                write_summary, digests, start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=30), maximum_attempts=3))
        except ActivityError as e:  # Ollama down all night: the brief still goes out, written by the template
            s = {"lines": brief.template(digests), "by": "template", "tries": 0, "rejected": [f"model unavailable: {e.cause}"]}
        took = (workflow.now() - started).total_seconds()
        md = brief.render(digests, s, f"Night Shift · Temporal run {workflow.info().workflow_id} · {took:.0f} s · summary by {s['by']}")
        return await workflow.execute_activity(
            deliver, DeliverIn(md, until.isoformat(), s["lines"].splitlines()[0], n.notify),
            start_to_close_timeout=timedelta(minutes=1), retry_policy=RetryPolicy(maximum_attempts=5))


def load_targets(path: str) -> list[dict]:
    return json.loads(Path(path).read_text())
