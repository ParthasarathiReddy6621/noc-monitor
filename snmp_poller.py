"""
SNMP polling of IF-MIB interface data (SNMP v1 / v2c).

Quick test against one device:
    python snmp_poller.py 192.168.100.1 public
    python snmp_poller.py 127.0.0.1 public --port 1161     (simulator)
"""

import argparse
import asyncio
import time
from datetime import datetime

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType,
    SnmpEngine, UdpTransportTarget, bulk_walk_cmd, get_cmd, walk_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

import db
from bandwidth import calc_rates, fmt_bps
from config import slug

# ---------------------------------------------------------------- OIDs
SYSTEM_OIDS = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",     # TimeTicks, hundredths of a second
    "sys_name": "1.3.6.1.2.1.1.5.0",
}

IF_TABLE = "1.3.6.1.2.1.2.2.1"             # IF-MIB::ifTable   (old, 32-bit)
IFX_TABLE = "1.3.6.1.2.1.31.1.1.1"         # IF-MIB::ifXTable  (64-bit counters)

INTERFACE_COLUMNS = {
    "descr":         f"{IF_TABLE}.2",      # ifDescr
    "speed":         f"{IF_TABLE}.5",      # ifSpeed (bps, max ~4.29 Gbps)
    "admin_status":  f"{IF_TABLE}.7",      # ifAdminStatus 1=up 2=down
    "oper_status":   f"{IF_TABLE}.8",      # ifOperStatus  1=up 2=down ...
    "in_octets32":   f"{IF_TABLE}.10",     # ifInOctets
    "in_errors":     f"{IF_TABLE}.14",     # ifInErrors
    "out_octets32":  f"{IF_TABLE}.16",     # ifOutOctets
    "out_errors":    f"{IF_TABLE}.20",     # ifOutErrors
    "name":          f"{IFX_TABLE}.1",     # ifName  e.g. Gi0/0
    "in_octets64":   f"{IFX_TABLE}.6",     # ifHCInOctets
    "out_octets64":  f"{IFX_TABLE}.10",    # ifHCOutOctets
    "high_speed":    f"{IFX_TABLE}.15",    # ifHighSpeed (Mbps)
    "alias":         f"{IFX_TABLE}.18",    # ifAlias = interface description
}

OPER_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown",
               5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

_MISSING = (NoSuchObject, NoSuchInstance, EndOfMibView)


class SnmpError(Exception):
    pass


def _convert(value):
    """pysnmp value -> plain Python int / str."""
    if value.__class__.__name__ in ("OctetString", "DisplayString"):
        return value.prettyPrint()
    try:
        return int(value)
    except (TypeError, ValueError):
        return value.prettyPrint()


# ---------------------------------------------------------------- SNMP calls
async def _get(engine, auth, target, oids):
    err_ind, err_status, err_index, var_binds = await get_cmd(
        engine, auth, target, ContextData(), *[ObjectType(ObjectIdentity(o)) for o in oids])
    if err_ind:
        raise SnmpError(str(err_ind))
    if err_status:
        raise SnmpError(f"{err_status.prettyPrint()} at index {err_index}")
    return [None if isinstance(v, _MISSING) else _convert(v) for _, v in var_binds]


async def _walk_column(engine, auth, target, column_oid, use_bulk):
    """Walk one table column -> {ifIndex: value}. Empty dict if not supported."""
    values = {}
    walker = (bulk_walk_cmd(engine, auth, target, ContextData(), 0, 25,
                            ObjectType(ObjectIdentity(column_oid)), lexicographicMode=False)
              if use_bulk else
              walk_cmd(engine, auth, target, ContextData(),
                       ObjectType(ObjectIdentity(column_oid)), lexicographicMode=False))
    async for err_ind, err_status, err_index, var_binds in walker:
        # IMPORTANT: on timeout the walker keeps yielding empty rows - stop on any error
        if err_ind:
            raise SnmpError(str(err_ind))
        if err_status:
            break
        for oid, value in var_binds:
            if isinstance(value, _MISSING):
                continue
            values[tuple(oid)[-1]] = _convert(value)
    return values


async def _poll(address, community, version, port, timeout, retries):
    engine = SnmpEngine()
    try:
        auth = CommunityData(community, mpModel=0 if version == "1" else 1)
        target = await UdpTransportTarget.create((address, port), timeout=timeout, retries=retries)

        descr, uptime, name = await _get(engine, auth, target, list(SYSTEM_OIDS.values()))
        system = {"sys_descr": descr, "sys_uptime": uptime or 0, "sys_name": name}

        use_bulk = version != "1"               # GETBULK only exists in v2c/v3
        columns = {}
        for key, oid in INTERFACE_COLUMNS.items():
            columns[key] = await _walk_column(engine, auth, target, oid, use_bulk)
        return system, columns
    finally:
        engine.close_dispatcher()


def _build_interfaces(columns):
    interfaces = []
    for idx in sorted(columns["descr"]):
        hc_in, hc_out = columns["in_octets64"].get(idx), columns["out_octets64"].get(idx)
        if hc_in is not None and hc_out is not None:
            in_oct, out_oct, bits = hc_in, hc_out, 64
        else:
            in_oct = columns["in_octets32"].get(idx, 0)
            out_oct = columns["out_octets32"].get(idx, 0)
            bits = 32

        high_speed = columns["high_speed"].get(idx)   # Mbps, correct for >4 Gbps links
        speed_bps = high_speed * 1_000_000 if high_speed else columns["speed"].get(idx, 0)

        interfaces.append({
            "if_index": idx,
            "if_name": columns["name"].get(idx) or columns["descr"][idx],
            "if_alias": columns["alias"].get(idx, ""),
            "admin_status": columns["admin_status"].get(idx, 4),
            "oper_status": columns["oper_status"].get(idx, 4),
            "speed_bps": speed_bps,
            "counter_bits": bits,
            "in_octets": in_oct,
            "out_octets": out_oct,
            "in_errors": columns["in_errors"].get(idx, 0),
            "out_errors": columns["out_errors"].get(idx, 0),
        })
    return interfaces


def poll_device(address, community="public", version="2c", port=161, timeout=2, retries=1):
    """Synchronous wrapper (safe to call from scheduler threads)."""
    system, columns = asyncio.run(_poll(address, community, str(version), port, timeout, retries))
    return system, _build_interfaces(columns)


# ---------------------------------------------------------------- scheduled check
def poll_and_store(name, address, community="public", version="2c", port=161,
                   timeout=2, retries=1, interfaces=None):
    """
    Called by the scheduler. Stores one interface_stats row per interface and
    returns the device result, plus one 'iface' result per admin-up interface.
    """
    try:
        system, iface_list = poll_device(address, community, version, port, timeout, retries)
    except Exception as e:
        return {"status": "DOWN", "response_ms": None,
                "detail": f"SNMP failed: {e} (check IP, community, ACL, UDP {port})"[:200]}

    epoch = time.time()
    timestamp = datetime.now().isoformat(timespec="seconds")
    wanted = set(interfaces or [])
    sub_results = []

    for iface in iface_list:
        if wanted and iface["if_name"] not in wanted and str(iface["if_index"]) not in wanted:
            continue

        sample = {**iface, "epoch": epoch, "timestamp": timestamp,
                  "device": name, "sys_uptime": system["sys_uptime"]}
        prev = db.get_last_interface_sample(name, iface["if_index"])
        sample.update(calc_rates(prev, sample))
        db.save_interface_sample(sample)

        # Admin-down ("shutdown") interfaces are intentional: don't report them as failures
        if iface["admin_status"] != 1:
            continue
        oper = OPER_STATUS.get(iface["oper_status"], "unknown")
        traffic = (f"in {fmt_bps(sample['in_bps'])} ({sample['in_util_pct']}%) / "
                   f"out {fmt_bps(sample['out_bps'])} ({sample['out_util_pct']}%)"
                   if sample["in_bps"] is not None else sample["note"])
        errors = f", +{sample['in_errors_delta']} in-errors" if sample["in_errors_delta"] else ""
        sub_check = {"id": f"iface-{slug(name)}-{iface['if_index']}", "type": "iface",
                     "name": name, "target": f"{iface['if_name']} {iface['if_alias']}".strip()}
        utils = [u for u in (sample["in_util_pct"], sample["out_util_pct"]) if u is not None]
        err_deltas = [e for e in (sample["in_errors_delta"], sample["out_errors_delta"]) if e is not None]
        sub_results.append((sub_check, {
            "status": "UP" if iface["oper_status"] == 1 else "DOWN",
            "response_ms": None,
            "detail": f"oper {oper}, {traffic}{errors}",
            # used by the alert engine for utilisation / error thresholds
            "metrics": {"util_pct": max(utils) if utils else None,
                        "errors_delta": sum(err_deltas) if err_deltas else None},
        }))

    uptime_h = system["sys_uptime"] / 100 / 3600
    return {"status": "UP", "response_ms": None,
            "detail": f"{system['sys_name']}, up {uptime_h:.1f}h, {len(iface_list)} interfaces",
            "sub_results": sub_results}


# ---------------------------------------------------------------- CLI test
def main():
    parser = argparse.ArgumentParser(description="Test SNMP polling against one device")
    parser.add_argument("address")
    parser.add_argument("community", nargs="?", default="public")
    parser.add_argument("--port", type=int, default=161)
    parser.add_argument("--version", default="2c", choices=["1", "2c"])
    args = parser.parse_args()

    try:
        system, ifaces = poll_device(args.address, args.community, args.version, args.port)
    except Exception as e:
        raise SystemExit(f"SNMP failed: {e}")

    print(f"Device : {system['sys_name']}")
    print(f"Uptime : {system['sys_uptime'] / 100 / 3600:.2f} hours")
    print(f"Descr  : {str(system['sys_descr'])[:80]}\n")
    print(f"{'Idx':>4} {'Name':<14} {'Admin':<6} {'Oper':<15} {'Speed':>10} {'Bits':>4} "
          f"{'InOctets':>16} {'OutOctets':>16} {'InErr':>6}  Alias")
    for i in ifaces:
        print(f"{i['if_index']:>4} {i['if_name'][:14]:<14} "
              f"{'up' if i['admin_status'] == 1 else 'down':<6} "
              f"{OPER_STATUS.get(i['oper_status'], '?'):<15} {fmt_bps(i['speed_bps']):>10} "
              f"{i['counter_bits']:>4} {i['in_octets']:>16} {i['out_octets']:>16} "
              f"{i['in_errors']:>6}  {i['if_alias']}")


if __name__ == "__main__":
    main()
