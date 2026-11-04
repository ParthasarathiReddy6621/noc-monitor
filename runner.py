"""Runs checks, stores results, and schedules each check on its own interval."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPool
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

import checks
import db
import snmp_poller
from alerts import AlertEngine

ALERTS = None   # set by init_alerting()


def init_alerting(settings):
    """Create the alert engine from the 'alerting' section of hosts.yaml."""
    global ALERTS
    ALERTS = AlertEngine(settings.get("alerting") or {})
    return ALERTS

CHECK_FUNCTIONS = {
    "ping": checks.ping_host,
    "port": checks.check_port,
    "http": checks.check_http,
    "dns": checks.check_dns,
    "snmp": snmp_poller.poll_and_store,
}


def _record(check, result, timestamp):
    """Save one result and print one status line."""
    db.save_result(check, result, timestamp)
    ms = f"{result['response_ms']} ms" if result["response_ms"] is not None else "-"
    flag = "OK  " if result["status"] == "UP" else "FAIL"
    print(f"{timestamp} [{flag}] {check['type']:<5} {check['name']:<22} "
          f"{check['target'][:40]:<40} {ms:>10}  {result['detail']}", flush=True)


def run_check(check):
    """Run one check, save it (plus any per-interface results), return the result."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    try:
        result = CHECK_FUNCTIONS[check["type"]](**check["params"])
    except Exception as e:                      # a buggy check must never kill the scheduler
        result = {"status": "DOWN", "response_ms": None, "detail": f"check error: {e}"[:200]}

    sub_results = result.pop("sub_results", [])   # SNMP: one result per interface
    _record(check, result, timestamp)
    _alert(check, result)
    for sub_check, sub_result in sub_results:
        if check.get("severity"):
            sub_check.setdefault("severity", check["severity"])   # interfaces inherit device severity
        _record(sub_check, sub_result, timestamp)
        _alert(sub_check, sub_result)
    return result


def _alert(check, result):
    if ALERTS is None:
        return
    try:
        ALERTS.process(check, result)
    except Exception as e:                      # alerting must never break monitoring
        print(f"[alerts] error for {check['id']}: {e}", flush=True)


def run_all(check_list):
    """Run every check once, in parallel."""
    with ThreadPoolExecutor(max_workers=10) as pool:
        return list(pool.map(run_check, check_list))


def build_scheduler(check_list, blocking=False, settings=None):
    """One interval job per check. First run happens immediately.
    If alerting.daily_report is set (e.g. "09:00"), also schedule the daily report."""
    scheduler_cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = scheduler_cls(
        # Use the system's current UTC offset directly. Auto-detection fails on some
        # systems (e.g. 'Asia/Calcutta' or Windows without tzdata).
        timezone=datetime.now().astimezone().tzinfo,
        executors={"default": APSThreadPool(20)},
        job_defaults={
            "coalesce": True,          # if runs were missed, run once (not N times)
            "max_instances": 1,        # never run the same check twice at once
            "misfire_grace_time": 30,
        },
    )
    for check in check_list:
        scheduler.add_job(
            run_check, "interval", seconds=check["interval"], args=[check],
            id=check["id"], name=check["id"], next_run_time=datetime.now(),
        )

    report_time = ((settings or {}).get("alerting") or {}).get("daily_report")
    if report_time and ALERTS is not None:
        import reports
        hour, minute = (int(x) for x in str(report_time).split(":"))
        scheduler.add_job(reports.send_daily_report, "cron", hour=hour, minute=minute,
                          args=[ALERTS], id="daily-report", name="daily-report")
        print(f"Daily report scheduled at {hour:02d}:{minute:02d}", flush=True)
    return scheduler
