#!/bin/sh
#
# Local development: Redis + Minio + optional logs pod in Kubernetes;
# REST and worker run on your laptop (see instructions printed at the end).
#
# Prerequisites:
#   - kubectl configured (Docker Desktop K8s, minikube, or GKE context)
#   - This script deploys Minio via minio/minio-k8s-dev.yaml (namespace minio-ns).
#     On GKE you may already have Minio from the course tutorial; if minio-proj
#     already exists, skip re-applying that file or delete the old release first.
#
set -e

if ! command -v kubectl >/dev/null 2>&1; then
    echo "kubectl is not installed or not on your PATH."
    echo ""
    echo "Install it, then re-run this script:"
    echo "  macOS (Homebrew):  brew install kubectl"
    echo "  Google Cloud SDK:  gcloud components install kubectl"
    echo "  Docker Desktop:    enable Kubernetes in Settings, then add to PATH, e.g.:"
    echo "                     export PATH=\"/Applications/Docker.app/Contents/Resources/bin:\$PATH\""
    echo ""
    exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "kubectl cannot reach a Kubernetes API server (often shows as localhost:8080 refused)."
    echo ""
    echo "Fix: point kubectl at a running cluster, then re-run this script."
    echo ""
    echo "  Docker Desktop: Settings → Kubernetes → Enable, wait until green, then:"
    echo "    kubectl config use-context docker-desktop"
    echo ""
    echo "  Minikube:"
    echo "    minikube start"
    echo ""
    echo "  GKE (use your cluster name and zone):"
    echo "    gcloud container clusters get-credentials CLUSTER_NAME --zone ZONE"
    echo ""
    echo "Current context: $(kubectl config current-context 2>/dev/null || echo '(none)')"
    exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "==> Minio (namespace minio-ns, service minio-proj)"
kubectl apply -f minio/minio-k8s-dev.yaml
echo "    Waiting for Minio pod to be Ready..."
kubectl wait --for=condition=ready pod -l app=minio -n minio-ns --timeout=180s

echo "==> Redis, logs, Minio ExternalName in default (not deploying rest/worker pods)"
kubectl apply -f redis/redis-deployment.yaml
kubectl apply -f redis/redis-service.yaml
kubectl apply -f logs/logs-deployment.yaml
kubectl apply -f minio/minio-external-service.yaml

echo "    Waiting for Redis pod to be Ready (needed before port-forward to redis)..."
if ! kubectl wait --for=condition=ready pod -l app=redis --timeout=300s; then
    echo ""
    echo "Redis did not become Ready in time. Status:"
    kubectl get pods -l app=redis -o wide
    echo ""
    kubectl describe pod -l app=redis | tail -40
    echo ""
    echo "If the pod is Pending, Docker Desktop may be low on RAM — increase Resources in"
    echo "Docker Desktop → Settings → Resources, or delete other pods: kubectl get pods -A"
    exit 1
fi

# Let Service endpoints catch up (avoids "pod is not running" / Pending races)
sleep 2

echo ""
echo "==> Starting port-forwards in the background (Redis 6379; Minio 9000+9001 in one process)"
kubectl port-forward --address 127.0.0.1 service/redis 6379:6379 &
PF_REDIS=$!
kubectl port-forward -n minio-ns --address 127.0.0.1 service/minio-proj 9000:9000 9001:9001 &
PF_MINIO=$!

echo "Port-forward PIDs: redis=$PF_REDIS minio=$PF_MINIO"
echo "To stop them later: kill $PF_REDIS $PF_MINIO"
echo ""

cat <<'EOF'
-------------------------------------------------------------------
If you already deployed rest/worker into the cluster (e.g. deploy-local-dev.sh),
remove them so local Python can own port 5000 and avoid duplicate workers:

  kubectl delete deployment rest worker --ignore-not-found

-------------------------------------------------------------------
Next steps (three terminals on your machine):

  Terminal A — REST API (listens on :5000)
    cd rest && pip3 install -r requirements.txt && python3 rest-server.py

  Terminal B — worker (DEMUCS; needs ~6GB RAM free)
    cd worker && pip3 install -r requirements.txt && python3 worker-server.py

  Terminal C — optional: watch logs from Redis
    cd logs && pip3 install redis && REDIS_HOST=127.0.0.1 REDIS_PORT=6379 python3 logs.py

  Terminal D — test (after placing a short MP3 under data/ e.g. data/short1.mp3)
    pip3 install requests jsonpickle
    python3 short-sample-request.py

Environment: REST defaults to localhost:5000. Minio credentials default to
rootuser / rootpass123 (match GKE-COMMANDS Helm example).
-------------------------------------------------------------------
EOF
