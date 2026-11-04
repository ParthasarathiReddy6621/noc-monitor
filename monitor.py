"""
NOC Monitor - command line

Usage:
    python monitor.py              # run every check once
    python monitor.py --schedule   # run checks on their intervals (Ctrl+C to stop)
    python monitor.py --report     # uptime % for the last 24 hours
    python monitor.py --report --hours 1
    python monitor.py --interfaces # SNMP interface status + bandwidth
    python monitor.py --incidents  # alert incidents (open + recent)
    python monitor.py --test-alert # send a test notification to all channels
"""

import argparse

import db
import runner
from bandwidth import fmt_bps
from config import load_config
from snmp_poller import OPER_STATUS


def print_report(hours):
    rows = db.get_uptime(hours)
    print(f"\n=== Uptime, last {hours}h ===")
    print(f"{'Type':<5} {'Name':<22} {'Target':<32} {'Checks':>6} {'Uptime%':>8} {'Avg ms':>9}")
    for r in rows:
        print(f"{r['check_type']:<5} {r['name']:<22} {r['target'][:32]:<32} "
              f"{r['checks']:>6} {r['uptime_pct']:>8} {str(r['avg_response_ms']):>9}")
    if not rows:
        print("No data yet - run some checks first.")


def print_interfaces():
    rows = db.get_latest_interfaces()
    print("\n=== Interfaces (latest SNMP poll) ===")
    print(f"{'Device':<12} {'Interface':<12} {'Admin':<5} {'Oper':<14} {'Speed':>10} "
          f"{'In':>12} {'Out':>12} {'In%':>6} {'Out%':>6} {'+Err':>5}  Description")
    for r in rows:
        print(f"{r['device'][:12]:<12} {r['if_name'][:12]:<12} "
              f"{'up' if r['admin_status'] == 1 else 'down':<5} "
              f"{OPER_STATUS.get(r['oper_status'], '?'):<14} {fmt_bps(r['speed_bps']):>10} "
              f"{fmt_bps(r['in_bps']):>12} {fmt_bps(r['out_bps']):>12} "
              f"{str(r['in_util_pct'] if r['in_util_pct'] is not None else '-'):>6} "
              f"{str(r['out_util_pct'] if r['out_util_pct'] is not None else '-'):>6} "
              f"{str(r['in_errors_delta'] if r['in_errors_delta'] is not None else '-'):>5}  "
              f"{r['if_alias'] or ''}")
    if not rows:
        print("No SNMP data yet - add snmp_devices to hosts.yaml and run checks.")
    elif any(r["in_bps"] is None for r in rows):
        print("\n(Bandwidth needs two polls - run the checks again, or use --schedule.)")


def print_incidents():
    rows = db.list_incidents(None, 30)
    print("\n=== Incidents (latest 30) ===")
    print(f"{'#':>4} {'Status':<13} {'Sev':<8} {'Condition':<11} {'Name':<16} {'Opened':<20} {'Resolved':<20} Detail")
    for r in rows:
        flap = " (flapping)" if r["flapping"] else ""
        print(f"{r['id']:>4} {r['status']:<13} {r['severity']:<8} {r['condition']:<11} "
              f"{r['name'][:16]:<16} {r['opened_at']:<20} {str(r['resolved_at'] or '-'):<20} "
              f"{(r['detail'] or '')[:50]}{flap}")
    if not rows:
        print("No incidents yet.")
    s = db.incident_summary()
    print(f"\nActive by severity: {s['active_by_severity'] or 'none'}   "
          f"MTTA: {s['mtta_minutes']} min   MTTR: {s['mttr_minutes']} min")


def main():
    parser = argparse.ArgumentParser(description="NOC Monitor")
    parser.add_argument("--schedule", action="store_true", help="run checks continuously")
    parser.add_argument("--report", action="store_true", help="show uptime report")
    parser.add_argument("--interfaces", action="store_true", help="show SNMP interface table")
    parser.add_argument("--incidents", action="store_true", help="show alert incidents")
    parser.add_argument("--test-alert", action="store_true", help="send a test notification")
    parser.add_argument("--hours", type=int, default=24, help="report window (default 24)")
    args = parser.parse_args()

    db.init_db()

    if args.report:
        print_report(args.hours)
        return
    if args.interfaces:
        print_interfaces()
        return
    if args.incidents:
        print_incidents()
        return

    settings, check_list = load_config()
    engine = runner.init_alerting(settings)

    if args.test_alert:
        print(f"Sending test alert to: {', '.join(engine.notifier.channels)}")
        engine.send_test()
        engine.notifier.flush()
        return

    if args.schedule:
        print(f"Scheduling {len(check_list)} checks. Ctrl+C to stop.")
        scheduler = runner.build_scheduler(check_list, blocking=True, settings=settings)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            print("\nStopped.")
    else:
        print(f"Running {len(check_list)} checks once...")
        results = runner.run_all(check_list)
        up = sum(r["status"] == "UP" for r in results)
        print(f"\nDone: {up} UP, {len(results) - up} DOWN")
        print("(Alerts need several failed checks in a row - use --schedule to see them fire.)")
        engine.notifier.flush()


if __name__ == "__main__":
    main()
