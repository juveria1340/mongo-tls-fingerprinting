"""
Generates a fake PCAP + ground truth with the same shape the cluster produces,
so you can test extract_features.py and train_rf.py before the cluster is up.

usage: python tests/make_synthetic.py data/synthetic
"""
import json
import os
import random
import socket
import sys
import time

import dpkt

MONGO = "10.244.0.10"
PODS = {"user": ["10.244.0.21", "10.244.0.22"], "txn": ["10.244.0.31", "10.244.0.32"],
        "auth": ["10.244.0.41", "10.244.0.42"]}
OPS = {"user": ["insert", "find_one", "update", "delete"],
       "txn": ["insert", "find_many", "aggregate"],
       "auth": ["insert", "find_one", "update", "delete"]}
# (request size range, response packets, response size range) — rough TLS shapes
SHAPE = {"insert": ((300, 1900), 1, (150, 190)), "find_one": ((180, 260), 1, (250, 2100)),
         "find_many": ((200, 280), 3, (800, 9000)), "aggregate": ((420, 520), 1, (300, 900)),
         "update": ((260, 2300), 1, (190, 230)), "delete": ((170, 230), 1, (150, 170))}
SVC_BIAS = {"user": 0, "txn": 40, "auth": 90}  # different collection/field names -> offsets


def pkt(src, sport, dst, dport, payload):
    tcp = dpkt.tcp.TCP(sport=sport, dport=dport, flags=dpkt.tcp.TH_ACK | dpkt.tcp.TH_PUSH,
                       data=payload)
    ip = dpkt.ip.IP(src=socket.inet_aton(src), dst=socket.inet_aton(dst), p=6, data=tcp)
    ip.len = len(ip)
    return bytes(dpkt.ethernet.Ethernet(type=dpkt.ethernet.ETH_TYPE_IP, data=ip))


def main(out):
    os.makedirs(f"{out}/pcap", exist_ok=True)
    random.seed(1)
    ports = {ip: random.randint(40000, 60000) for ips in PODS.values() for ip in ips}
    t = time.time()
    gt = []
    with open(f"{out}/pcap/mongo-synthetic.pcap", "wb") as fh:
        w = dpkt.pcap.Writer(fh)
        for _ in range(4000):
            svc = random.choice(list(PODS))
            ip = random.choice(PODS[svc])
            op = random.choice(OPS[svc])
            (rq_lo, rq_hi), n_resp, (rs_lo, rs_hi) = SHAPE[op]
            b = SVC_BIAS[svc]
            t += random.uniform(0.005, 0.05)
            t0 = t
            t += random.uniform(0.0001, 0.0004)
            w.writepkt(pkt(ip, ports[ip], MONGO, 27017,
                           os.urandom(random.randint(rq_lo, rq_hi) + b)), t)
            t += random.uniform(0.0005, 0.004 if op != "aggregate" else 0.012)
            for _ in range(random.randint(1, n_resp)):
                w.writepkt(pkt(MONGO, 27017, ip, ports[ip],
                               os.urandom(random.randint(rs_lo, rs_hi) + b)), t)
                t += random.uniform(0.00005, 0.0003)
            gt.append({"gt": 1, "service": svc, "op": op, "pod_ip": ip,
                       "t_start": int(t0 * 1e9), "t_end": int(t * 1e9)})
            # occasional monitoring heartbeat on a different socket
            if random.random() < 0.02:
                w.writepkt(pkt(ip, ports[ip] + 1, MONGO, 27017, os.urandom(300)), t + 0.001)
    os.makedirs(f"{out}/gt", exist_ok=True)
    with open(f"{out}/gt/ground_truth.jsonl", "w") as fh:
        fh.writelines(json.dumps(g) + "\n" for g in gt)
    print(f"wrote {len(gt)} synthetic operations to {out}/")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/synthetic")
