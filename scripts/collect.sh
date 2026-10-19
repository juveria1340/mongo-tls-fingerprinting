#!/usr/bin/env bash
# Pulls PCAPs from the capture sidecar and ground-truth lines from every API pod.
set -euo pipefail
NS=fingerprint
mkdir -p data/pcap data/gt

POD=$(kubectl -n $NS get pod -l app=mongo -o jsonpath='{.items[0].metadata.name}')
for f in $(kubectl -n $NS exec "$POD" -c capture -- ls /captures); do
  echo "copying $f"
  kubectl -n $NS cp -c capture "$POD:/captures/$f" "data/pcap/$f"
done

: > data/gt/ground_truth.jsonl
for p in $(kubectl -n $NS get pod -l tier=api -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n $NS logs "$p" | grep '"gt": 1' >> data/gt/ground_truth.jsonl || true
done

echo "pcaps:        $(ls data/pcap | wc -l) file(s), $(du -sh data/pcap | cut -f1)"
echo "ground truth: $(wc -l < data/gt/ground_truth.jsonl) labelled operations"
