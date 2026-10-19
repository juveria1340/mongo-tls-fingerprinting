"""
PCAP + ground truth  ->  features.csv  (one row per labelled MongoDB operation)

Only metadata an on-path observer sees is used: TLS-encrypted TCP payload sizes,
directions and timing. No decryption, no client-side timing.

usage: python extract_features.py data/pcap data/gt/ground_truth.jsonl data/features.csv
"""
import bisect
import glob
import json
import os
import socket
import sys
from collections import defaultdict

import dpkt
import numpy as np
import pandas as pd

MONGO_PORT = 27017


def read_pcaps(pcap_dir):
    """Return {(client_ip, client_port): sorted list of (ts, dir, payload_len)}.
    dir = +1 client->mongo (request), -1 mongo->client (response)."""
    flows = defaultdict(list)
    files = sorted(glob.glob(os.path.join(pcap_dir, "*.pcap")))
    if not files:
        sys.exit(f"no .pcap files in {pcap_dir}")
    for path in files:
        with open(path, "rb") as fh:
            reader = dpkt.pcap.Reader(fh)
            link = reader.datalink()
            for ts, buf in reader:
                try:
                    if link == dpkt.pcap.DLT_LINUX_SLL:
                        ip = dpkt.sll.SLL(buf).data
                    elif link == 276:  # LINUX_SLL2 (tcpdump -i any)
                        ip = dpkt.ip.IP(buf[20:])
                    else:
                        ip = dpkt.ethernet.Ethernet(buf).data
                    if not isinstance(ip, dpkt.ip.IP) or not isinstance(ip.data, dpkt.tcp.TCP):
                        continue
                    tcp = ip.data
                except (dpkt.dpkt.UnpackError, IndexError):
                    continue
                n = len(tcp.data)
                if n == 0:  # pure ACKs carry no information about the operation
                    continue
                if tcp.dport == MONGO_PORT:
                    key, d = (socket.inet_ntoa(ip.src), tcp.sport), +1
                elif tcp.sport == MONGO_PORT:
                    key, d = (socket.inet_ntoa(ip.dst), tcp.dport), -1
                else:
                    continue
                flows[key].append((float(ts), d, n))
    for k in flows:
        flows[k].sort()
    return flows


def app_flow_per_ip(flows):
    """PyMongo opens extra monitoring sockets; the app connection is the one with
    the most bytes. Pick it per client IP."""
    best = {}
    for (ip, port), pkts in flows.items():
        vol = sum(p[2] for p in pkts)
        if ip not in best or vol > best[ip][1]:
            best[ip] = (port, vol)
    return {ip: port for ip, (port, _) in best.items()}


def stats(prefix, arr):
    if len(arr) == 0:
        return {f"{prefix}_{s}": 0.0 for s in ("min", "max", "mean", "std", "sum")}
    a = np.asarray(arr, dtype=float)
    return {f"{prefix}_min": a.min(), f"{prefix}_max": a.max(), f"{prefix}_mean": a.mean(),
            f"{prefix}_std": a.std(), f"{prefix}_sum": a.sum()}


def featurise(pkts):
    ts = np.array([p[0] for p in pkts])
    d = np.array([p[1] for p in pkts])
    n = np.array([p[2] for p in pkts])
    up, down = n[d == 1], n[d == -1]
    f = {
        "n_pkts": len(pkts), "n_up": len(up), "n_down": len(down),
        "duration_ms": (ts[-1] - ts[0]) * 1e3 if len(ts) > 1 else 0.0,
        "first_up_len": up[0] if len(up) else 0,
        "first_down_len": down[0] if len(down) else 0,
        "last_down_len": down[-1] if len(down) else 0,
        "down_up_byte_ratio": down.sum() / up.sum() if up.sum() else 0.0,
        "n_direction_changes": int(np.sum(d[1:] != d[:-1])) if len(d) > 1 else 0,
    }
    # server "think time": first response after first request
    if len(up) and len(down):
        t_up0 = ts[d == 1][0]
        later = ts[(d == -1) & (ts >= t_up0)]
        f["server_latency_ms"] = (later[0] - t_up0) * 1e3 if len(later) else 0.0
    else:
        f["server_latency_ms"] = 0.0
    iat = np.diff(ts) * 1e3 if len(ts) > 1 else []
    f.update(stats("up_len", up))
    f.update(stats("down_len", down))
    f.update(stats("iat_ms", iat))
    return f


def main(pcap_dir, gt_path, out_csv):
    flows = read_pcaps(pcap_dir)
    app_port = app_flow_per_ip(flows)
    index = {k: [p[0] for p in v] for k, v in flows.items()}
    print(f"flows: {len(flows)}  client IPs: {len(app_port)}")

    rows, empty, unknown = [], 0, 0
    with open(gt_path) as fh:
        for line in fh:
            try:
                g = json.loads(line[line.index("{"):])
            except ValueError:
                continue
            port = app_port.get(g["pod_ip"])
            if port is None:
                unknown += 1
                continue
            key = (g["pod_ip"], port)
            t0, t1 = g["t_start"] / 1e9, g["t_end"] / 1e9
            lo = bisect.bisect_left(index[key], t0)
            hi = bisect.bisect_right(index[key], t1)
            pkts = flows[key][lo:hi]
            if not pkts:
                empty += 1
                continue
            row = featurise(pkts)
            row.update(service=g["service"], op=g["op"], label=f"{g['service']}:{g['op']}",
                       pod_ip=g["pod_ip"], t_start=g["t_start"])
            rows.append(row)

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    df.to_csv(out_csv, index=False)
    print(f"wrote {len(df)} rows -> {out_csv}  (skipped: {empty} empty windows, "
          f"{unknown} pods not seen in capture)")
    if len(df):
        print(df["label"].value_counts().to_string())


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
