# Encrypted MongoDB Traffic Fingerprinting in Kubernetes

**Question:** if traffic between microservices and MongoDB is encrypted with TLS, can someone watching the cluster network still tell *what the database is doing*?

**Answer (in this testbed): yes.** From encrypted packet sizes and timing alone, without decrypting anything, a Random Forest identified the MongoDB operation type with near-perfect accuracy and the originating microservice with 88% accuracy (vs a 45% majority baseline), across 52,419 labelled operations.

---

## Architecture

```
        Traffic Generator (k8s Job, async Python)
                         │  HTTP
        ┌────────────────┼────────────────┐
     User API      Transaction API     Auth API        ← FastAPI, 2 replicas each
        └────────────────┼────────────────┘              each logs ground truth per DB call
                         │  MongoDB wire protocol over TLS (requireTLS)
                      MongoDB ── tcpdump sidecar (shares the pod network, port 27017 only)
                         │
                     PCAP files
                         │
            extract_features.py   (30 side-channel features per operation)
                         │
                train_rf.py       (Random Forest, 3 targets)
                         │
              Classification reports, confusion matrices, feature importances
```

| Service | Endpoints | MongoDB operations |
|---|---|---|
| User API | `POST/GET/PUT/DELETE /users` | insert, find_one, update, delete |
| Transaction API | `POST /transactions`, `GET /transactions`, `GET /transactions/summary/{id}` | insert, find_many, aggregate |
| Auth API | `POST/GET/PUT/DELETE /sessions` | insert, find_one, update, delete |

## Repository layout

| Path | Purpose |
|---|---|
| `services/app.py` | One FastAPI image for all three services (`SERVICE=user\|txn\|auth`). Every Mongo call is timed and logged as a ground-truth label. |
| `traffic/generator.py` | Async workload: weighted, randomised workflows with variable payload sizes. |
| `k8s/` | kind cluster config, MongoDB + capture sidecar, API deployments, traffic Job. |
| `scripts/gen-certs.sh` | Creates a throwaway CA and server certificate and loads them as Kubernetes secrets. |
| `scripts/collect.sh` | Copies PCAPs off the sidecar and pulls ground-truth labels from pod logs. |
| `analysis/extract_features.py` | Matches each labelled time window to that pod's packets and computes features. |
| `analysis/train_rf.py` | Random Forest for `op`, `service`, `service:op`: reports, 5-fold CV, plots. |
| `tests/make_synthetic.py` | Fake PCAP + labels for testing the analysis without a cluster. |

---

## Setup (Windows 10/11 with WSL2)

### 1. WSL2 + Ubuntu
In PowerShell (Administrator):
```powershell
wsl --install -d Ubuntu
wsl -l -v                      # VERSION must be 2
```
If Ubuntu already exists on version 1: `wsl --set-version Ubuntu 2`.

### 2. Docker Desktop
1. Install and start Docker Desktop.
2. **Settings → General:** tick *Use the WSL 2 based engine*.
3. **Settings → Resources → WSL Integration:** enable **Ubuntu** → *Apply & restart*.
4. Optional: give WSL more memory by creating `C:\Users\<you>\.wslconfig`:
   ```
   [wsl2]
   memory=6GB
   processors=4
   ```
   then `wsl --shutdown` and restart Docker Desktop.

Docker Desktop must stay running (it can sit minimised in the tray) whenever the cluster is in use.

### 3. Tools inside Ubuntu
All commands from here on run in the **Ubuntu terminal**, not PowerShell.
```bash
sudo apt update && sudo apt install -y make unzip curl openssl python3-venv python3-pip

curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install kubectl /usr/local/bin/ && rm kubectl

curl -Lo kind https://kind.sigs.k8s.io/dl/v0.24.0/kind-linux-amd64
sudo install kind /usr/local/bin/ && rm kind

docker ps && kubectl version --client && kind version   # all three should succeed
```

### 4. Get the project into Ubuntu's filesystem
Keep it in the Linux home folder, not under `/mnt/c` (much slower there).
```bash
cp /mnt/c/Users/<you>/Downloads/mongo-fingerprint.zip ~
cd ~ && unzip mongo-fingerprint.zip && cd mongo-fingerprint
```

---

## Running the pipeline

| Stage | Command | What happens | Done when |
|---|---|---|---|
| 1. Python env | `make venv && source .venv/bin/activate` | Installs analysis dependencies | Prompt shows `(.venv)` |
| 2. Dry run | `make test-pipeline` | Synthetic PCAP → features → RF | Three reports in `results/synthetic/` |
| 3. Cluster | `make cluster` | Creates a single-node kind cluster | `kubectl get nodes` shows `Ready` |
| 4. TLS | `make certs` | CA + server cert as k8s secrets | "certs created" |
| 5. Images | `make build` | Builds API + generator images, loads into kind | No errors |
| 6. Deploy | `make deploy` then `make status` | MongoDB (TLS-only) + 3 APIs × 2 replicas | All `Running`; mongo pod `2/2` |
| 7. Smoke test | `make smoke` | One real request end to end | `{"gt": 1, ...}` lines printed |
| 8. Traffic | `make traffic` then `make watch` | 30-minute workload, 6 virtual users | Log ends with `DONE {...}` |
| 9. Collect | `make collect` | PCAPs → `data/pcap/`, labels → `data/gt/` | Counts printed |
| 10. Features | `make features` | `data/features.csv`, one row per operation | Row count ≈ label count |
| 11. Train | `make train` | Reports + plots in `results/cluster/` | `summary.csv` written |

Notes:
- `make watch` only follows the logs; Ctrl+C doesn't stop the job. Check status with `kubectl -n fingerprint get job traffic` (`1/1` = finished).
- Every new terminal: `cd ~/mongo-fingerprint && source .venv/bin/activate`.
- To view results in Windows: `explorer.exe "$(wslpath -w results/cluster)"`, or copy them out with `cp -r results/cluster /mnt/c/Users/<you>/Downloads/fingerprint-results`.
- Fresh dataset: `make reset-data`, then repeat from stage 8. Tear down: `make clean`.

---

## How ground truth is produced

1. Each API pod holds **one** pooled MongoDB connection (`maxPoolSize=1`) and a lock, so only one operation is ever in flight per TCP flow.
2. `t_start`/`t_end` are taken with nanosecond precision immediately around each PyMongo call and logged as JSON.
3. The capture runs on the MongoDB pod, so every packet of an operation falls inside its client-side window.
4. On a single-node kind cluster all pods share one kernel clock, so log and PCAP timestamps are directly comparable.
5. PyMongo's monitoring sockets are filtered out by taking each pod's highest-volume flow as the application connection.
6. The TLS handshake happens at start-up, so it never falls inside a labelled window.

Features use **only** what an on-path observer can see: encrypted payload sizes, packet directions and timing.

---

## Results

Dataset: **52,419** labelled operations from a 30-minute run (6 virtual users, 3 services × 2 replicas).
Model: Random Forest, 300 trees, balanced class weights; 75/25 stratified split plus 5-fold cross-validation.

| Target | Classes | Test accuracy | Test macro F1 | 5-fold CV macro F1 | Majority baseline |
|---|---|---|---|---|---|
| Operation type (`op`) | 6 | **1.000** | 1.000 | 1.000 ± 0.0001 | 0.492 |
| Originating service (`service`) | 3 | **0.881** | 0.881 | 0.879 ± 0.003 | 0.451 |
| Service + operation (`label`) | 11 | **0.876** | 0.927 | 0.925 ± 0.002 | 0.301 |

### Interpretation

- **Operation type is fully exposed.** The model separates all six operations without error, and the result is stable across folds.
- **Service identity leaks substantially.** 88% against a 45% baseline. Transaction-API traffic is easy to spot (it alone uses `find_many` and `aggregate`). User and Auth share the same four operations, so most remaining errors are likely between those two (see `confusion_service.png`).
- **Combined label:** because `op` is always correct, every `label` error is a service error. Accuracy (0.876) is below macro F1 (0.927), meaning errors concentrate in the largest classes (most likely inserts, which look alike across services), while small classes such as `txn:aggregate` are near-perfect.

### Why operation type is 100%: the size signatures

Per-operation range of the first encrypted request (`first_up_len`), first encrypted reply (`first_down_len`) and payload-carrying packets per operation (`n_pkts`), in bytes:

| Operation | Request size | Reply size | Packets | Distinguishing signal |
|---|---|---|---|---|
| aggregate | **340 (fixed)** | 128–493 | 2 | Identical pipeline every call → constant request size |
| find_many | **191 (fixed)** | 128–4344 | 2–4 | Fixed query; reply grows with results and may span several TCP segments |
| update | 250–2257 | **82 (fixed)** | 2 | Reply carries an extra `nModified` field → 15 bytes longer than other write acks |
| find_one | 194–229 | **244–2276** | 2 | Small request, large reply (the document) |
| insert | **244–1784** | **67 (fixed)** | 2 | Large request, minimal `{n, ok}` acknowledgement |
| delete | **205–240** | **67 (fixed)** | 2 | Small request, same minimal acknowledgement |

TLS adds a near-constant overhead, so encrypted length closely tracks plaintext length. MongoDB's reply formats are fixed per operation, so each operation leaves a distinct `(request size, reply size)` signature. The leak is deterministic enough that a handful of rules reproduces the Random Forest:

```
request == 340                  → aggregate
request == 191                  → find_many
reply   == 82                   → update
reply   == 67 and request ≥ 244 → insert
reply   == 67 and request ≤ 240 → delete
otherwise                       → find_one
```

The weakest boundary is **insert vs delete**: identical 67-byte replies, with requests separated by only 4 bytes (delete ≤ 240, insert ≥ 244). That margin is a property of this workload's document sizes. An application inserting very small documents would overlap with deletes.

### Time-based validation

Training on the first 70% of the run (by time) and testing on the last 30% guards against optimistic random splits.

| Target | Time-split accuracy |
|---|---|
| `op` | _TODO: fill in from check 2_ |
| `service` | _TODO: fill in from check 2_ |

Command:
```bash
python -c "
import pandas as pd; from sklearn.ensemble import RandomForestClassifier as RF
df=pd.read_csv('data/features.csv').sort_values('t_start'); X=df.drop(columns=['service','op','label','pod_ip','t_start'])
c=int(len(df)*.7)
for t in ['op','service']:
    m=RF(300,n_jobs=-1,random_state=42).fit(X[:c],df[t][:c]); print(t, round(m.score(X[c:],df[t][c:]),4))"
```

### Output files (`results/cluster/`)

| File | Contents |
|---|---|
| `summary.csv` | Headline metrics per target (table above) |
| `report_{op,service,label}.txt` | Per-class precision / recall / F1 |
| `confusion_{op,service,label}.png` | Row-normalised confusion matrices |
| `importance_{op,service,label}.png` | Random Forest feature importances |

---

## Threats to validity

- **Lab conditions:** single-node cluster with near-zero network latency and no competing traffic.
- **One operation in flight per connection:** real applications pipeline and overlap requests, which blurs operation boundaries.
- **Synthetic workload:** document sizes come from the generator's distributions; the insert/delete margin depends on them.
- **Application-specific signatures:** exact byte sizes depend on collection and field names, so an attacker would need to profile the target application first.
- **Random split:** the headline table uses a stratified random split; see the time-based validation above.
- **No service-mesh mTLS** layered on top of MongoDB TLS.

## Next steps

- **Defence evaluation:** pad messages to fixed buckets (e.g. multiples of 512 bytes) and measure how far accuracy falls.
- **Feature ablation:** size-only vs timing-only features.
- **Concurrency:** raise `WORKERS` and replicas, allow multiple in-flight operations per connection.
- **Multi-node / CloudLab:** the same manifests on kubeadm, with chrony time sync and a fixed capture node.
- **Temporal model:** per-packet sequences as input to a 1D-CNN.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `connectex` / `localhost:8080` errors from kubectl | You're in PowerShell, or the cluster isn't created yet. Use the Ubuntu terminal and run `make cluster`. |
| `docker ps`: cannot connect to the Docker daemon | Start Docker Desktop and enable WSL Integration for Ubuntu. |
| Mongo pod `1/2` or CrashLoop | `kubectl -n fingerprint logs deploy/mongo -c mongo`. Usually the TLS secret is missing: `make certs` then `kubectl -n fingerprint rollout restart deploy/mongo`. |
| `ErrImageNeverPulled` / `ImagePullBackOff` | Re-run `make build` (the `kind load` step). |
| Many "empty windows" during feature extraction | Check `ls data/pcap` isn't empty and the capture container hasn't restarted. |
| `explorer.exe` opens Documents | The folder doesn't exist yet, or use `explorer.exe "$(wslpath -w results/cluster)"`. |
