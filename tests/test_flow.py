"""The workflow's promises, on Temporal's time-skipping test server with stand-in activities:
a database that can't be read becomes a finding, and the brief still goes out when the model is down."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from nightshift.flow import QUEUE, CollectIn, DeliverIn, NightIn, NightShift

DELIVERED: list[DeliverIn] = []


@activity.defn(name="read_logs")
def read_logs(a: CollectIn) -> dict:
    if a.target["name"] == "gone":
        raise RuntimeError("No such container: gone")
    return {"name": a.target["name"], "window": "w", "alive": True, "alive_note": "", "log_lines": 1, "routine": 0,
            "last_alert_log_entry": "x", "findings": [{"severity": "critical", "what": "Instance terminated", "count": 1,
                                                       "first": "2026-10-20 18:39 UTC", "last": "2026-10-20 18:39 UTC",
                                                       "codes": [], "sample": "", "do": "Start it."}]}


@activity.defn(name="write_summary")
def model_down(digests: list[dict]) -> dict:
    raise ConnectionError("Ollama is not running")


@activity.defn(name="deliver")
def deliver(a: DeliverIn) -> str:
    DELIVERED.append(a)
    return "briefs/test.md"


@pytest.mark.asyncio
async def test_brief_arrives_with_a_dead_host_and_no_model():
    DELIVERED.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[NightShift],
                          activities=[read_logs, model_down, deliver], activity_executor=ThreadPoolExecutor(4)):
            out = await env.client.execute_workflow(
                NightShift.run, NightIn([{"name": "lab-se2"}, {"name": "gone"}], 24, "2026-10-21T11:00:00+00:00", False),
                id=str(uuid.uuid4()), task_queue=QUEUE)
    assert out == "briefs/test.md"
    md = DELIVERED[0].markdown
    assert "Couldn't read the logs" in md and "No such container: gone" in md  # retried, then reported, not a crash
    assert "Instance terminated" in md
    assert "summary by template" in md  # model down all night: the template wrote the two lines
    assert DELIVERED[0].headline.startswith("Critical findings on lab-se2, gone")
