# Encrypted MongoDB Traffic Fingerprinting in Kubernetes — one-day build

Can an on-path observer tell **which MongoDB operation** a microservice performed, and
**which service** performed it, from TLS-encrypted traffic alone?

```
Traffic Generator (k8s Job)
        │  HTTP
  ┌─────┼──────────────┐
User API  Transaction API  Auth API      ← each logs ground truth: {service, op, pod_ip, t_start, t_end}
  └─────┼──────────────┘
        │  MongoDB wire protocol over TLS (requireTLS)
     MongoDB  ── tcpdump sidecar (same netns, port 27017 only)
        │
   PCAP files ──► extract_features.py ──► features.csv ──► train_rf.py ──► classification reports
```

## Layout

| Path | What it is |
|---|---|
| `services/app.py` | One FastAPI image, three services (`SERVICE=user\|txn\|auth`). Every Mongo call is timed and logged as a ground-truth label. |
| `traffic/generator.py` | Async workload: weighted, randomised workflows with variable payload sizes. |
| `k8s/` | kind config, MongoDB + capture sidecar, API deployments, traffic Job. |
| `scripts/gen-certs.sh` | Throwaway CA + server cert → Kubernetes secrets. |
| `scripts/collect.sh` | Copies PCAPs off the sidecar, pulls labels from pod logs. |
| `analysis/extract_features.py` | Matches each label's time window to packets on that pod's app connection; 30 side-channel features (sizes, directions, timing). |
| `analysis/train_rf.py` | Random Forest for `op`, `service`, `service:op`; reports, 5-fold CV, confusion matrices, feature importances. |
| `tests/make_synthetic.py` | Fake PCAP + labels so you can test the analysis before the cluster exists. |

## Prerequisites

Docker, `kind`, `kubectl`, `make`, Python 3.10+. On Windows, run everything from **WSL2** (Docker Desktop with WSL integration on).

## The day

| Time | Block | Commands | Done when |
|---|---|---|---|
| 0:00–0:30 | Setup | `make venv && source .venv/bin/activate`<br>`make test-pipeline` | Synthetic run prints three reports into `results/synthetic/` |
| 0:30–1:30 | Cluster | `make cluster certs build deploy`<br>`make status` | All pods `Running`, mongo pod shows `2/2` |
| 1:30–2:00 | Smoke test | `make smoke` | You see `{"gt": 1, ...}` lines |
| 2:00–2:45 | Read the code | Walk through `app.py` → `extract_features.py` | You can explain the labelling method (below) |
| 2:45–3:30 | Collect | `make traffic` then `make watch` (30 min) | Job prints `DONE` |
| 3:30–4:00 | Extract | `make collect features` | `features.csv` has ≥90 % of labels matched |
| 4:00–4:30 | Train | `make train` | `results/cluster/` has reports + PNGs |
| 4:30–6:30 | Experiments | see *Extensions* — pick one or two | An extra results table |
| 6:30–8:00 | Write-up | Method, results table, confusion matrices, threats to validity | Push to GitHub |

Lunch and breaks fit in the slack; the collection run is a natural break.

## How labelling works (the bit examiners will ask about)

1. Each API pod uses **one** pooled Mongo connection (`maxPoolSize=1`) and a lock, so only one operation is ever in flight per TCP flow.
2. `t_start`/`t_end` are recorded on the client immediately around the PyMongo call. The capture sits on the Mongo pod, so every packet of that operation falls inside the window.
3. In kind every pod shares the host kernel clock, so log and PCAP timestamps are directly comparable.
4. PyMongo also opens monitoring sockets; the extractor picks each pod's highest-volume flow as the app connection, which filters heartbeats out.
5. The TLS handshake happens at startup (`warm_up`), so it never lands inside a labelled window.

Only payload sizes, directions and timing are used as features. Client-side timing is never fed to the model.

## Troubleshooting

- **Mongo pod `1/2` or CrashLoop** → `kubectl -n fingerprint logs deploy/mongo -c mongo`. Usually the `mongo-tls` secret is missing: rerun `make certs` and `kubectl -n fingerprint rollout restart deploy/mongo`.
- **APIs not ready** → `kubectl -n fingerprint logs deploy/user-api`. A TLS hostname error means the cert SANs and `MONGO_URI` disagree.
- **`ErrImageNeverPulled` / `ImagePullBackOff` on fp-api** → rerun `make build` (the `kind load` step).
- **Many "empty windows" in extraction** → check `ls data/pcap` isn't empty and that the capture container hasn't restarted (`kubectl -n fingerprint get pod -l app=mongo`); restarts create new files, which `collect.sh` picks up.
- **Want a fresh dataset** → `make reset-data`, then `make traffic` again.

## Extensions (afternoon)

- **Harder split**: train on the first 70 % of time, test on the last 30 % (sort by `t_start`) — more honest than random splitting.
- **Defence evaluation**: pad request/response sizes (e.g. add a random `pad` field to every document) and rerun; show how far accuracy falls.
- **Feature ablation**: size-only vs timing-only features.
- **Scale**: raise `WORKERS` or `replicas` and see whether concurrency on the server hurts accuracy.
- **CloudLab**: same manifests on a kubeadm cluster; enable chrony on every node and pin Mongo with a `nodeSelector` so the capture point is fixed.
- **Temporal model**: the per-packet sequences in `extract_features.read_pcaps` are the natural input for a 1D-CNN.

## Threats to validity (write these down)

Single-node cluster and loopback-like latency; synthetic workload; one in-flight op per connection (real apps pipeline more); payload sizes drawn from the generator's distributions; no service mesh mTLS layer on top.
