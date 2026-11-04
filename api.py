"""
NOC Monitor - REST API + background scheduler

Run:   python api.py
Open:  http://127.0.0.1:8000/docs   (interactive API documentation)
"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import db
import reports
import runner
from bandwidth import fmt_bps
from config import BASE_DIR, load_config
from snmp_poller import OPER_STATUS

SETTINGS, CHECKS = load_config()
CHECKS_BY_ID = {c["id"]: c for c in CHECKS}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables and start running checks in the background
    db.init_db()
    runner.init_alerting(SETTINGS)
    scheduler = runner.build_scheduler(CHECKS, settings=SETTINGS)
    scheduler.start()
    print(f"Scheduler started with {len(CHECKS)} checks")
    yield
    # Shutdown
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="NOC Monitor API",
    description="Ping, TCP port, HTTP and DNS monitoring with SQLite history.",
    version="0.5.0",
    lifespan=lifespan,
)


def _summary(rows):
    up = sum(r["status"] == "UP" for r in rows)
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(rows), "up": up, "down": len(rows) - up}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", response_class=HTMLResponse, tags=["dashboard"])
def dashboard():
    """The NOC dashboard web page."""
    return (BASE_DIR / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api", tags=["info"])
def api_info():
    return {"service": "NOC Monitor", "version": app.version, "docs": "/docs",
            "endpoints": ["/health", "/checks", "/status", "/status/down",
                          "/status/{check_id}", "/uptime", "/interfaces",
                          "/interfaces/{device}/{if_index}/history",
                          "/incidents", "/incidents/summary", "POST /incidents/{id}/ack",
                          "POST /alerts/test", "POST /checks/{check_id}/run",
                          "/dashboard", "/timeseries", "/metrics", "/reports/daily"]}


@app.get("/health", tags=["info"])
def health():
    """Is the monitor itself alive?"""
    return {"status": "ok", "checks_configured": len(CHECKS)}


@app.get("/checks", tags=["config"])
def list_checks():
    """All configured checks (from hosts.yaml)."""
    return [{k: c[k] for k in ("id", "type", "name", "target", "interval")} for c in CHECKS]


@app.get("/status", tags=["status"])
def current_status():
    """Latest result of every check - the NOC status board."""
    rows = db.get_latest()
    return {**_summary(rows), "checks": rows}


@app.get("/status/down", tags=["status"])
def down_checks():
    """Only checks that are currently DOWN."""
    rows = db.get_latest()
    down = [r for r in rows if r["status"] == "DOWN"]
    return {**_summary(rows), "checks": down}


@app.get("/status/{check_id}", tags=["status"])
def check_history(check_id: str, limit: int = Query(50, ge=1, le=1000)):
    """Recent history for one check (newest first). Works for iface-* ids too."""
    check = CHECKS_BY_ID.get(check_id)
    if not check and not db.check_id_exists(check_id):
        raise HTTPException(404, f"Unknown check '{check_id}'. See /checks or /status for ids.")
    info = ({k: check[k] for k in ("id", "type", "name", "target", "interval")}
            if check else {"id": check_id})
    return {"check": info, "history": db.get_history(check_id, limit)}


def _iface_view(r):
    return {**r,
            "oper_status_text": OPER_STATUS.get(r["oper_status"], "unknown"),
            "admin_status_text": "up" if r["admin_status"] == 1 else "down",
            "in_rate": fmt_bps(r["in_bps"]), "out_rate": fmt_bps(r["out_bps"]),
            "speed": fmt_bps(r["speed_bps"])}


@app.get("/interfaces", tags=["snmp"])
def interfaces(device: Optional[str] = None):
    """Latest status, bandwidth and utilisation of every SNMP interface."""
    return [_iface_view(r) for r in db.get_latest_interfaces(device)]


@app.get("/interfaces/{device}/{if_index}/history", tags=["snmp"])
def interface_history(device: str, if_index: int, limit: int = Query(60, ge=1, le=5000)):
    """Bandwidth history of one interface (newest first) - ready for graphing."""
    rows = db.get_interface_history(device, if_index, limit)
    if not rows:
        raise HTTPException(404, f"No data for {device} ifIndex {if_index}. See /interfaces.")
    return {"device": device, "if_index": if_index, "if_name": rows[0]["if_name"],
            "samples": [{k: r[k] for k in ("timestamp", "oper_status", "in_bps", "out_bps",
                                           "in_util_pct", "out_util_pct",
                                           "in_errors_delta", "out_errors_delta", "note")}
                        for r in rows]}


@app.get("/uptime", tags=["status"])
def uptime(hours: int = Query(24, ge=1, le=720)):
    """Uptime % and average response time per check."""
    return {"window_hours": hours, "checks": db.get_uptime(hours)}


@app.post("/checks/{check_id}/run", tags=["config"])
def run_now(check_id: str):
    """Run one check immediately (useful while troubleshooting)."""
    check = CHECKS_BY_ID.get(check_id)
    if not check:
        raise HTTPException(404, f"Unknown check '{check_id}'.")
    return {"check_id": check_id, **runner.run_check(check)}


@app.get("/incidents", tags=["alerts"])
def incidents(status: str = Query("active", description="active, open, acknowledged, resolved or all"),
              limit: int = Query(100, ge=1, le=1000)):
    """Incidents created by the alert engine (newest first)."""
    return db.list_incidents(None if status == "all" else status, limit)


@app.get("/incidents/summary", tags=["alerts"])
def incidents_summary():
    """Active incidents by severity, plus MTTA / MTTR in minutes."""
    return db.incident_summary()


@app.post("/incidents/{incident_id}/ack", tags=["alerts"])
def acknowledge(incident_id: int, by: str = "noc-engineer"):
    """Acknowledge an open incident ("I'm working on it")."""
    if not db.get_incident(incident_id):
        raise HTTPException(404, f"Incident {incident_id} not found")
    if not db.acknowledge_incident(incident_id, by):
        raise HTTPException(409, "Incident is not OPEN (already acknowledged or resolved)")
    return db.get_incident(incident_id)


@app.post("/alerts/test", tags=["alerts"])
def test_alert():
    """Send a test message to every configured channel (console/Telegram/email)."""
    runner.ALERTS.send_test()
    return {"sent_to": list(runner.ALERTS.notifier.channels)}


@app.get("/timeseries", tags=["dashboard"])
def timeseries(hours: int = Query(6, ge=1, le=168), bucket_minutes: int = Query(5, ge=1, le=240)):
    """Average response time and availability per check, bucketed for charts."""
    return db.get_timeseries(hours, bucket_minutes)


@app.get("/reports/daily", tags=["reports"])
def daily_report_preview(hours: int = Query(24, ge=1, le=720)):
    """Preview the daily report text (does not send it)."""
    return reports.build_daily_report(hours)


@app.post("/reports/daily", tags=["reports"])
def daily_report_send(hours: int = Query(24, ge=1, le=720)):
    """Send the daily report now to every channel (console/Telegram/email)."""
    return reports.send_daily_report(runner.ALERTS, hours)


@app.get("/metrics", response_class=PlainTextResponse, tags=["dashboard"])
def metrics():
    """Prometheus-format metrics, so Grafana/Prometheus can scrape this monitor."""
    out = ["# HELP noc_check_up 1 if the last check result was UP, else 0",
           "# TYPE noc_check_up gauge"]
    rows = db.get_latest()
    for r in rows:
        labels = f'check_id="{r["check_id"]}",name="{r["name"]}",type="{r["check_type"]}"'
        out.append(f"noc_check_up{{{labels}}} {1 if r['status'] == 'UP' else 0}")
    out += ["# HELP noc_check_response_ms Last response time in milliseconds",
            "# TYPE noc_check_response_ms gauge"]
    for r in rows:
        if r["response_ms"] is not None:
            labels = f'check_id="{r["check_id"]}",name="{r["name"]}",type="{r["check_type"]}"'
            out.append(f"noc_check_response_ms{{{labels}}} {r['response_ms']}")
    out += ["# HELP noc_incidents_active Open or acknowledged incidents by severity",
            "# TYPE noc_incidents_active gauge"]
    for sev, n in db.incident_summary()["active_by_severity"].items():
        out.append(f'noc_incidents_active{{severity="{sev}"}} {n}')
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import os
    # NOC_API_HOST=0.0.0.0 makes the API reachable from other machines (e.g. when run on the GNS3 VM)
    uvicorn.run(app, host=os.environ.get("NOC_API_HOST", "127.0.0.1"), port=8000)
