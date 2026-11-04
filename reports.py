"""Daily NOC report: current status + last 24 h incidents, sent to every channel."""

import db


def build_daily_report(hours=24):
    latest = db.get_latest()
    up = sum(r["status"] == "UP" for r in latest)
    down = [r for r in latest if r["status"] == "DOWN"]
    stats = db.incident_stats_since(hours)
    active = db.list_incidents("active", 100)

    lines = [
        f"Devices/checks UP now: {up}/{len(latest)}",
        f"Availability (last {hours}h): {stats['availability_pct']}%",
        f"Incidents (last {hours}h): {stats['opened']} opened, {stats['resolved']} resolved"
        + (f", {stats['critical']} critical" if stats["critical"] else ""),
        f"MTTR: {stats['mttr_minutes']} min" if stats["mttr_minutes"] is not None else "MTTR: -",
    ]
    if stats["flapping"]:
        lines.append(f"Flapping incidents: {stats['flapping']}")
    if stats["most_affected"]:
        lines.append("Most affected: " + ", ".join(f"{n} ({c})" for n, c in stats["most_affected"]))
    if down:
        lines.append("DOWN right now: " + ", ".join(f"{r['name']} ({r['check_type']})" for r in down))
    if active:
        lines.append(f"Open incidents: {len(active)} "
                     f"({sum(1 for a in active if a['status'] == 'ACKNOWLEDGED')} acknowledged)")
    else:
        lines.append("Open incidents: none - all clear")

    title = "Daily NOC report" + (" - ALL CLEAR" if not down and not active else "")
    return {"title": title, "body": "\n".join(lines), "stats": stats,
            "up": up, "total": len(latest)}


def send_daily_report(engine, hours=24):
    report = build_daily_report(hours)
    engine.notifier.notify("REPORT", "info", report["title"], report["body"], force=True)
    return report
