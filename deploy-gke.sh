#!/usr/bin/env bash
#
# One-shot GKE deploy for lab7-demucs (build images → push GCR → apply manifests → ingress).
#
# Prerequisites: gcloud CLI, kubectl, docker, helm; billing enabled; Container Registry or Artifact Registry.
#
# Usage:
#   export PROJECT_ID=corded-nature-379302
#   export CLUSTER_NAME=mykube
#   export ZONE=us-central1-a
#   ./deploy-gke.sh
#
# Options:
#   --create-cluster          Create a new GKE cluster (default: off; you use an existing cluster)
#   --machine-type TYPE       With --create-cluster only (default: n1-standard-4)
#   --num-nodes N             With --create-cluster only (default: 2)
#   --skip-build              Do not docker build/push (YAML must already point at correct images)
#   --skip-minio              Do not install/upgrade Helm MinIO
#   -h, --help                Show this help
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

CREATE_CLUSTER=0
SKIP_BUILD=0
SKIP_MINIO=0
MACHINE_TYPE="n1-standard-4"
NUM_NODES=2

PROJECT_ID="${PROJECT_ID:-}"
CLUSTER_NAME="${CLUSTER_NAME:-mykube}"
ZONE="${ZONE:-us-central1-a}"

usage() {
  sed -n '1,25p' "$0" | tail -n +2
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --create-cluster) CREATE_CLUSTER=1 ;;
    --machine-type) MACHINE_TYPE="$2"; shift ;;
    --num-nodes) NUM_NODES="$2"; shift ;;
    --skip-build) SKIP_BUILD=1 ;;
    --skip-minio) SKIP_MINIO=1 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set PROJECT_ID (e.g. export PROJECT_ID=corded-nature-379302)"
  exit 1
fi

echo "==> Project: $PROJECT_ID  Cluster: $CLUSTER_NAME  Zone: $ZONE"
echo "==> Repo root: $ROOT"

echo "==> gcloud config"
gcloud config set project "$PROJECT_ID"

if [[ "$CREATE_CLUSTER" -eq 1 ]]; then
  echo "==> Creating cluster (this takes several minutes)..."
  gcloud container clusters create "$CLUSTER_NAME" \
    --zone "$ZONE" \
    --num-nodes "$NUM_NODES" \
    --machine-type "$MACHINE_TYPE" \
    --release-channel regular
fi

echo "==> Cluster credentials"
gcloud container clusters get-credentials "$CLUSTER_NAME" --zone "$ZONE"

echo "==> HTTP load balancing addon"
gcloud container clusters update "$CLUSTER_NAME" \
  --zone "$ZONE" \
  --update-addons=HttpLoadBalancing=ENABLED \
  || true

if [[ "$SKIP_MINIO" -eq 0 ]]; then
  echo "==> Helm: MinIO (namespace minio-ns, release minio-proj)"
  helm repo add bitnami https://charts.bitnami.com/bitnami 2>/dev/null || true
  helm repo update
  helm upgrade --install minio-proj bitnami/minio \
    --namespace minio-ns \
    --create-namespace \
    --set auth.rootUser=rootuser \
    --set auth.rootPassword=rootpass123 \
    --set defaultBuckets=queue,output \
    --wait --timeout 600s
  kubectl get svc -n minio-ns
fi

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  echo "==> Docker auth + build + push to gcr.io/$PROJECT_ID"
  gcloud auth configure-docker -q
  docker build -f "$ROOT/rest/Dockerfile-rest" -t demucs-rest:latest "$ROOT/rest"
  docker tag demucs-rest:latest "gcr.io/$PROJECT_ID/demucs-rest:latest"
  docker push "gcr.io/$PROJECT_ID/demucs-rest:latest"

  docker build -f "$ROOT/worker/Dockerfile" -t demucs-worker:latest "$ROOT/worker"
  docker tag demucs-worker:latest "gcr.io/$PROJECT_ID/demucs-worker:latest"
  docker push "gcr.io/$PROJECT_ID/demucs-worker:latest"
else
  echo "==> Skipping build/push (--skip-build)"
fi

echo "==> Patch deployment YAMLs to use GCR images"
REST_DEP="$ROOT/rest/rest-deployment.yaml"
WORKER_DEP="$ROOT/worker/worker-deployment.yaml"
if grep -q "gcr.io/$PROJECT_ID/demucs-rest" "$REST_DEP" 2>/dev/null; then
  echo "    (rest-deployment already references gcr.io/$PROJECT_ID)"
else
  sed -i.bak "s|image: demucs-rest:latest|image: gcr.io/$PROJECT_ID/demucs-rest:latest|" "$REST_DEP"
  sed -i.bak "s|imagePullPolicy: IfNotPresent|imagePullPolicy: Always|" "$REST_DEP"
fi
if grep -q "gcr.io/$PROJECT_ID/demucs-worker" "$WORKER_DEP" 2>/dev/null; then
  echo "    (worker-deployment already references gcr.io/$PROJECT_ID)"
else
  sed -i.bak "s|image: demucs-worker:latest|image: gcr.io/$PROJECT_ID/demucs-worker:latest|" "$WORKER_DEP"
  sed -i.bak "s|imagePullPolicy: IfNotPresent|imagePullPolicy: Always|" "$WORKER_DEP"
fi

echo "==> kubectl apply"
kubectl apply -f "$ROOT/redis/redis-deployment.yaml"
kubectl apply -f "$ROOT/redis/redis-service.yaml"
kubectl apply -f "$REST_DEP"
kubectl apply -f "$ROOT/rest/rest-service.yaml"
kubectl apply -f "$ROOT/logs/logs-deployment.yaml"
kubectl apply -f "$WORKER_DEP"
kubectl apply -f "$ROOT/minio/minio-external-service.yaml"
kubectl apply -f "$ROOT/rest/rest-ingress-gke.yaml"

echo ""
echo "==> Done. Rollout status:"
kubectl rollout status deployment/rest --timeout=180s || true
kubectl rollout status deployment/worker --timeout=300s || true

echo ""
echo "==> Ingress (wait for ADDRESS — can take several minutes):"
kubectl get ingress rest-ingress

IP="$(kubectl get ingress rest-ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
if [[ -n "$IP" ]]; then
  echo ""
  echo "REST (external):  http://${IP}/"
  echo "Queue:            curl -s \"http://${IP}/apiv1/queue\""
  echo "Export for tests: export REST=${IP}:80"
else
  echo "No IP yet. Run:  kubectl get ingress rest-ingress --watch"
fi

echo ""
echo "Logs:  kubectl logs -l app=rest -f"
echo "       kubectl logs -l app=worker -f"
