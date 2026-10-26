from datetime import datetime, timezone

import pytest

from nightshift import brief
from nightshift.logs import alert_entries, classify, group, listener_entries

ALERT = """2026-10-20T01:31:42.214243+00:00
Errors in file /opt/oracle/diag/rdbms/orclcdb/ORCLCDB/trace/ORCLCDB_j000_58811.trc:
ORA-12012: error on auto execute of job "SYS"."ORA$AT_OS_OPT_SY_780"
ORA-20001: Statistics Advisor: Invalid task name for the current user
2026-10-20T01:41:44.137859+00:00
ORA-12012: error on auto execute of job "SYS"."ORA$AT_OS_OPT_SY_782"
2026-10-20T02:01:44.201233+00:00
Resize operation completed for file# 3, old size 634880K, new size 645120K
2026-10-20T18:39:10.327137+00:00
Instance Critical Process (pid: 7, ospid: 3437, MMAN) died unexpectedly
PMON (ospid: 3425): terminating the instance due to error 822
Tue Oct 20 19:00:00 2026
ORA-01653: unable to extend table LAB.T by 128 in tablespace USERS
"""


def test_alert_entries_split_on_both_timestamp_styles():
    es = alert_entries(ALERT)
    assert len(es) == 5
    assert es[0].at == datetime(2026, 10, 20, 1, 31, 42, tzinfo=timezone.utc)
    assert es[-1].at == datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)  # 11g style


def test_group_folds_a_repeating_job_and_puts_critical_first():
    found, routine = group(alert_entries(ALERT))
    assert routine == 1
    assert [f.key for f in found] == ["instance_down", "space", "failed_job"]
    assert "MMAN) died" in found[0].sample  # the cause stays visible next to the outage
    job = found[-1]
    assert job.count == 2 and "SYS\"" not in job.detail and any("ORA$AT_OS_OPT_SY_*" in d for d in job.detail)


@pytest.mark.parametrize("line,key", [
    ("ORA-00600: internal error code, arguments: [kcbz]", "internal_error"),
    ("ORA-00060: Deadlock detected.", "deadlock"),
    ("ORA-1652: unable to extend temp segment", "space"),
    ("ORA-20001: Statistics Advisor", "other_error"),  # not ORA-20 (processes)
    ("ORA-06512: at line 1", "other_error"),
    ("Thread 1 advanced to log sequence 7514 (LGWR switch)", "noise"),
    ("FREEPDB1(3):ORA-01653: unable to increase tablespace NS_TINY [FREEPDB1]  by 1MB", "space"),
])
def test_rules(line, key):
    from nightshift.logs import Entry
    k, sev, *_ = classify(Entry(datetime.now(timezone.utc), line))
    assert (k if sev != "noise" else "noise") == key


def test_listener_service_died_is_critical():
    es = listener_entries("20-OCT-2026 18:39:11 * service_died * ORCLCDB * 12537\n"
                          "25-OCT-2026 22:17:02:632 * service_update * inst_upd=1 * FREE * 0\n")
    found, routine = group(es)
    assert routine == 1 and found[0].key == "listener_lost_db"


DIGEST = [{"name": "lab-se2", "window": "2026-10-20 11:00 to 2026-10-21 11:00 UTC", "alive": None, "alive_note": "",
           "log_lines": 3, "routine": 1, "last_alert_log_entry": "2026-10-20 18:39 UTC",
           "findings": [{"severity": "critical", "what": "Instance terminated", "count": 1, "first": "2026-10-20 18:39 UTC",
                         "last": "2026-10-20 18:39 UTC", "codes": [], "sample": "", "do": "Start it."}]}]


def test_summary_check_catches_invented_numbers_and_missing_names():
    assert brief.problems("lab-se2 died at 18:39.\nStart lab-se2.", DIGEST) == []
    assert "numbers not in the digest: 17" in brief.problems("lab-se2 died at 17:39.\nStart it.", DIGEST)[0]
    assert any("didn't name lab-se2" in p for p in brief.problems("A database died.\nStart it.", DIGEST))
    assert any("not 2" in p for p in brief.problems("lab-se2 died.", DIGEST))


def test_render_lists_every_critical_finding_even_if_the_summary_skips_it():
    md = brief.render(DIGEST, {"lines": "All quiet.\nKeep sleeping.", "by": "gemma4:e4b", "tries": 1})
    assert "Instance terminated" in md and "🔴" in md


def test_deadlock_trace_names_the_kind_and_both_statements():
    from pathlib import Path
    from nightshift.traces import deadlock
    d = deadlock((Path(__file__).parent / "data" / "deadlock_tx.trc").read_text())
    assert d["kind"].startswith("row lock deadlock")
    assert [(s["sid"], s["user"], s["sql"]) for s in d["sessions"]] == [
        (230, "LAB", "update ns_dl set v = 2 where id = 1"), (42, "LAB", "update ns_dl set v = 2 where id = 2")]


def test_deadlock_entry_keeps_its_trace_path():
    found, _ = group(alert_entries("2026-10-25T22:47:51.770225+00:00\nFREEPDB1(3):ORA-00060: Deadlock detected. "
                                   "More info in file /opt/oracle/diag/rdbms/free/FREE/trace/FREE_ora_56408.trc.\n"))
    assert found[0].traces == ["/opt/oracle/diag/rdbms/free/FREE/trace/FREE_ora_56408.trc"]
