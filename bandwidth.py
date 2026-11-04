"""
Bandwidth maths: turn two SNMP counter samples into bits per second.

SNMP interface counters (ifInOctets / ifHCInOctets) only ever count UP.
To get a rate you take two samples and divide the difference by the time:

    bps = (octets_now - octets_before) * 8 / seconds_between_polls

Things that can go wrong (and how we handle them):
  * Counter wrap   - a 32-bit counter rolls over to 0 after 4,294,967,295.
                     If now < before, assume it wrapped once and add 2^32.
  * Device reboot  - counters reset to 0. Detected by sysUpTime going DOWN.
                     The rate is unknown for that interval, so skip it.
  * Missed wraps   - at 1 Gbps a 32-bit counter wraps every ~34 s. If it
                     wraps twice between polls we can't tell. That's why we
                     prefer 64-bit (HC) counters. A rate above the interface
                     speed is treated as invalid.
"""


def fmt_bps(bps):
    """12_345_678 -> '12.35 Mbps'"""
    if bps is None:
        return "-"
    for unit, size in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if bps >= size:
            return f"{bps / size:.2f} {unit}"
    return f"{bps:.0f} bps"


def counter_delta(before, now, bits):
    """Difference between two counter readings, handling one wrap."""
    if now >= before:
        return now - before
    return (2 ** bits - before) + now


def calc_rates(prev, cur):
    """
    prev / cur: dicts with epoch, sys_uptime, counter_bits, speed_bps,
                in_octets, out_octets, in_errors, out_errors.
    Returns in/out bps, utilisation %, new errors, and a note.
    """
    empty = {"in_bps": None, "out_bps": None, "in_util_pct": None, "out_util_pct": None,
             "in_errors_delta": None, "out_errors_delta": None}

    if prev is None:
        return {**empty, "note": "first sample - rate available next poll"}

    seconds = cur["epoch"] - prev["epoch"]
    if seconds <= 0:
        return {**empty, "note": "invalid time between samples"}

    if cur["sys_uptime"] < prev["sys_uptime"]:
        return {**empty, "note": "device rebooted (sysUpTime went back) - counters reset"}

    if cur["counter_bits"] != prev["counter_bits"]:
        return {**empty, "note": "counter type changed (32/64-bit)"}

    bits = cur["counter_bits"]
    if bits == 64 and (cur["in_octets"] < prev["in_octets"] or cur["out_octets"] < prev["out_octets"]):
        # A 64-bit counter won't wrap in practice, so going backwards means a reset
        return {**empty, "note": "64-bit counter went backwards (counter reset)"}

    in_bps = counter_delta(prev["in_octets"], cur["in_octets"], bits) * 8 / seconds
    out_bps = counter_delta(prev["out_octets"], cur["out_octets"], bits) * 8 / seconds

    speed = cur["speed_bps"] or 0
    if speed and (in_bps > speed * 1.1 or out_bps > speed * 1.1):
        return {**empty, "note": "rate above interface speed (missed wrap or counter reset) - discarded"}

    return {
        "in_bps": round(in_bps, 2),
        "out_bps": round(out_bps, 2),
        "in_util_pct": round(in_bps / speed * 100, 2) if speed else None,
        "out_util_pct": round(out_bps / speed * 100, 2) if speed else None,
        # Error counters are always 32-bit
        "in_errors_delta": counter_delta(prev["in_errors"], cur["in_errors"], 32),
        "out_errors_delta": counter_delta(prev["out_errors"], cur["out_errors"], 32),
        "note": f"{bits}-bit counters over {seconds:.1f}s",
    }
