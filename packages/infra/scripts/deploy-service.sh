#!/usr/bin/env bash
set -euo pipefail

# Deploy a single ECS service: build Docker image → push to ECR → ECS force new deployment.
#
# Usage:
#   bash deploy-service.sh <service-name>
#   bash deploy-service.sh cc-headless
#   bash deploy-service.sh agent
#   bash deploy-service.sh healthcare
#   bash deploy-service.sh --list
#   bash deploy-service.sh --skip-build cc-headless
#   bash deploy-service.sh --status cc-headless

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$INFRA_DIR/../.." && pwd)"

NS=$(grep '^ns' "${INFRA_DIR}/.toml" | sed 's/.*= *"\(.*\)"/\1/')
STAGE=$(grep '^stage' "${INFRA_DIR}/.toml" | sed 's/.*= *"\(.*\)"/\1/')
REGION=$(grep '^region' "${INFRA_DIR}/.toml" | sed 's/.*= *"\(.*\)"/\1/')
PREFIX="${NS}${STAGE}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ECR_NS=$(echo "${PREFIX}" | tr '[:upper:]' '[:lower:]')
PLATFORM="linux/arm64"
IMAGE_TAG="latest"

lookup() {
  local svc=$1 field=$2
  case "${svc}:${field}" in
    agent:ctx)           echo "packages/agent" ;;
    agent:repo)          echo "${ECR_NS}/rca-agent" ;;
    agent:cluster)       echo "${PREFIX}RcaAgent" ;;
    agent:service)       echo "${PREFIX}RcaAgent" ;;
    cc-headless:ctx)     echo "packages/cc-headless" ;;
    cc-headless:repo)    echo "${ECR_NS}/cc-headless" ;;
    cc-headless:cluster) echo "${PREFIX}CcHeadless" ;;
    cc-headless:service) echo "${PREFIX}CcHeadless" ;;
    healthcare:ctx)      echo "packages/healthcare-sensor-app" ;;
    healthcare:repo)     echo "${ECR_NS}/healthcare" ;;
    healthcare:cluster)  echo "${PREFIX}Healthcare" ;;
    healthcare:service)  echo "${PREFIX}Healthcare" ;;
    *) echo "Unknown: ${svc}:${field}" >&2; return 1 ;;
  esac
}

ALL_SERVICES="agent cc-headless healthcare"

log() { echo -e "\033[1;34m▶ $*\033[0m"; }
err() { echo -e "\033[1;31m✗ $*\033[0m" >&2; }
ok()  { echo -e "\033[1;32m✓ $*\033[0m"; }

ecr_login() {
  log "ECR 로그인: ${ECR_REGISTRY}"
  aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null 2>&1
  ok "ECR 로그인 성공"
}

do_build() {
  local svc=$1
  local ctx repo image
  ctx=$(lookup "$svc" ctx)
  repo=$(lookup "$svc" repo)
  image="${ECR_REGISTRY}/${repo}:${IMAGE_TAG}"
  log "빌드: $svc → $image"
  docker build --platform "$PLATFORM" -t "$image" "${REPO_ROOT}/${ctx}"
  ok "빌드 완료: $svc"
}

do_push() {
  local svc=$1
  local repo image
  repo=$(lookup "$svc" repo)
  image="${ECR_REGISTRY}/${repo}:${IMAGE_TAG}"
  log "푸시: $image"
  docker push "$image"
  ok "푸시 완료: $svc"
}

do_ecs_deploy() {
  local svc=$1
  local cluster service_name
  cluster=$(lookup "$svc" cluster)
  service_name=$(lookup "$svc" service)
  log "ECS 재배포: $cluster / $service_name"
  aws ecs update-service \
    --cluster "$cluster" \
    --service "$service_name" \
    --force-new-deployment \
    --region "$REGION" \
    --query "service.{status:status,desired:desiredCount,running:runningCount}" \
    --output table
  ok "재배포 시작: $svc"
}

do_status() {
  local svc=$1
  local cluster service_name
  cluster=$(lookup "$svc" cluster)
  service_name=$(lookup "$svc" service)
  log "상태: $svc ($cluster)"
  aws ecs describe-services \
    --cluster "$cluster" \
    --services "$service_name" \
    --region "$REGION" \
    --query "services[0].{status:status,desired:desiredCount,running:runningCount,pending:pendingCount,deployments:deployments[*].{status:status,desired:desiredCount,running:runningCount,rollout:rolloutState}}" \
    --output yaml
}

# --- Parse args ---
while [[ $# -gt 0 && "$1" == "--" ]]; do shift; done

SKIP_BUILD=false
SHOW_STATUS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=true; shift ;;
    --status)     SHOW_STATUS=true; shift ;;
    --list)
      echo "Available services:"
      for s in $ALL_SERVICES; do echo "  $s"; done
      exit 0
      ;;
    --help|-h)
      echo "Usage: $0 [options] <service> [service...]"
      echo ""
      echo "Options:"
      echo "  --skip-build   ECR 이미지 빌드 없이 ECS만 재배포"
      echo "  --status       ECS 서비스 상태만 확인"
      echo "  --list         사용 가능한 서비스 목록"
      echo ""
      echo "Services: $ALL_SERVICES"
      exit 0
      ;;
    -*) err "Unknown option: $1"; exit 1 ;;
    *)  break ;;
  esac
done

if [[ $# -lt 1 ]]; then
  err "서비스 이름이 필요합니다. 사용 가능: $ALL_SERVICES"
  exit 1
fi

SERVICES=("$@")

for svc in "${SERVICES[@]}"; do
  lookup "$svc" ctx >/dev/null || { err "알 수 없는 서비스: $svc"; exit 1; }
done

if [[ "$SHOW_STATUS" == "true" ]]; then
  for svc in "${SERVICES[@]}"; do
    do_status "$svc"
  done
  exit 0
fi

if [[ "$SKIP_BUILD" == "false" ]]; then
  ecr_login
  for svc in "${SERVICES[@]}"; do
    do_build "$svc"
    do_push "$svc"
  done
else
  log "--skip-build: 빌드 생략"
fi

for svc in "${SERVICES[@]}"; do
  do_ecs_deploy "$svc"
done
