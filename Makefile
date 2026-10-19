NS      := fingerprint
CLUSTER := fingerprint
PY      := python3

.PHONY: help venv test-pipeline cluster certs build deploy status smoke traffic watch collect features train all clean reset-data

help:
	@echo "make venv           # local python env for analysis"
	@echo "make test-pipeline  # synthetic PCAP -> features -> RF (no cluster needed)"
	@echo "make cluster certs build deploy   # bring up the cluster"
	@echo "make smoke          # quick check that APIs write to MongoDB over TLS"
	@echo "make traffic        # 30-min traffic job;  make watch  to follow it"
	@echo "make collect features train       # PCAP + labels -> report"

venv:
	$(PY) -m venv .venv && .venv/bin/pip install -q -r analysis/requirements.txt
	@echo "activate with: source .venv/bin/activate"

test-pipeline:
	$(PY) tests/make_synthetic.py data/synthetic
	$(PY) analysis/extract_features.py data/synthetic/pcap data/synthetic/gt/ground_truth.jsonl data/synthetic/features.csv
	$(PY) analysis/train_rf.py data/synthetic/features.csv results/synthetic

cluster:
	kind create cluster --config k8s/kind-config.yaml

certs:
	bash scripts/gen-certs.sh

build:
	docker build -t fp-api:latest services
	docker build -t fp-traffic:latest traffic
	kind load docker-image fp-api:latest fp-traffic:latest --name $(CLUSTER)

deploy:
	kubectl apply -f k8s/mongo.yaml
	kubectl -n $(NS) rollout status deploy/mongo --timeout=180s
	kubectl apply -f k8s/apis.yaml
	kubectl -n $(NS) rollout status deploy/user-api deploy/txn-api deploy/auth-api --timeout=180s

status:
	kubectl -n $(NS) get pods -o wide

smoke:
	kubectl -n $(NS) run smoke --rm -i --restart=Never --image=curlimages/curl -- sh -c '\
	  curl -sf -XPOST user-api:8000/users -H "content-type: application/json" -d "{\"name\":\"a\",\"email\":\"a@b.c\"}" && echo && \
	  curl -sf txn-api:8000/healthz && echo && curl -sf auth-api:8000/healthz && echo'
	kubectl -n $(NS) logs -l tier=api --tail=5 | grep '"gt": 1'

traffic:
	kubectl -n $(NS) delete job traffic --ignore-not-found
	kubectl apply -f k8s/traffic-job.yaml

watch:
	kubectl -n $(NS) logs -f job/traffic

collect:
	bash scripts/collect.sh

features:
	$(PY) analysis/extract_features.py data/pcap data/gt/ground_truth.jsonl data/features.csv

train:
	$(PY) analysis/train_rf.py data/features.csv results/cluster

all: collect features train

reset-data:
	kubectl -n $(NS) rollout restart deploy/mongo deploy/user-api deploy/txn-api deploy/auth-api
	rm -rf data/pcap data/gt data/features.csv

clean:
	kind delete cluster --name $(CLUSTER)
