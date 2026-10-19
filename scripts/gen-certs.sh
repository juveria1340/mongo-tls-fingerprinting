#!/usr/bin/env bash
# Creates a throwaway CA + MongoDB server cert and loads them as Kubernetes secrets.
set -euo pipefail
NS=fingerprint
OUT=certs
mkdir -p "$OUT"
cd "$OUT"

openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ca.key -out ca.pem -subj "/CN=fingerprint-ca" 2>/dev/null

cat > san.cnf <<EOF
[req]
distinguished_name=dn
[dn]
[ext]
subjectAltName=DNS:mongo,DNS:mongo.${NS},DNS:mongo.${NS}.svc,DNS:mongo.${NS}.svc.cluster.local,DNS:localhost
EOF

openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=mongo.${NS}.svc.cluster.local" 2>/dev/null
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.crt -days 365 -extfile san.cnf -extensions ext 2>/dev/null
cat server.key server.crt > server.pem

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
kubectl -n "$NS" delete secret mongo-tls mongo-ca --ignore-not-found
kubectl -n "$NS" create secret generic mongo-tls --from-file=server.pem --from-file=ca.pem
kubectl -n "$NS" create secret generic mongo-ca --from-file=ca.pem
echo "certs created and loaded into namespace $NS"
