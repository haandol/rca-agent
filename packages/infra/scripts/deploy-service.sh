#!/usr/bin/env bash
set -euo pipefail

# Deploy a single ECS service: build Docker image → push to ECR → ECS force new deployment.
#
# Usage:
#   bash deploy-service.sh <service-name>
#   bash deploy-service.sh cc-headless
#   bash deploy-service.sh agent
#   bash deploy-service.sh healthcare
#   bash deploy-service.sh execution
#   bash deploy-service.sh cc-headless execution   # 같은 이미지, 두 진입점
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

# 이미지 태그는 커밋 SHA로 고정한다. 실행 중인 하네스 버전을 태그만으로
# 식별할 수 있어야 하므로 mutable 한 latest 를 배포 대상으로 쓰지 않는다.
# 커밋되지 않은 변경이 있으면 태그에 표시해 재현 불가 상태를 드러낸다.
resolve_image_tag() {
  local sha dirty=""
  sha=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null) || {
    err "git 저장소가 아니어서 이미지 태그를 고정할 수 없습니다"
    exit 1
  }
  if ! git -C "$REPO_ROOT" diff --quiet HEAD -- 2>/dev/null; then
    dirty="-dirty"
  fi
  echo "${sha}${dirty}"
}

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
    # 실행 워커는 분석 워커와 같은 이미지를 다른 진입점으로 띄운다. 그래서 빌드
    # 컨텍스트와 리포지토리가 cc-headless 와 동일하고, 배포 대상 스택만 다르다.
    execution:ctx)       echo "packages/cc-headless" ;;
    execution:repo)      echo "${ECR_NS}/cc-headless" ;;
    execution:cluster)   echo "${PREFIX}PlaybookExecution" ;;
    execution:service)   echo "${PREFIX}PlaybookExecution" ;;
    agent:stack)         echo "${PREFIX}RcaAgentServiceStack" ;;
    agent:tagenv)        echo "AGENT_IMAGE_TAG" ;;
    cc-headless:stack)   echo "${PREFIX}CcHeadlessStack" ;;
    cc-headless:tagenv)  echo "CC_HEADLESS_IMAGE_TAG" ;;
    healthcare:stack)    echo "${PREFIX}HealthcareServiceStack" ;;
    healthcare:tagenv)   echo "HEALTHCARE_IMAGE_TAG" ;;
    execution:stack)     echo "${PREFIX}PlaybookExecutionStack" ;;
    execution:tagenv)    echo "EXECUTION_IMAGE_TAG" ;;
    *) echo "Unknown: ${svc}:${field}" >&2; return 1 ;;
  esac
}

ALL_SERVICES="agent cc-headless healthcare execution"

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

# 지금 배포된 태스크 정의가 가리키는 이미지 태그.
deployed_tag() {
  local svc=$1 family
  family=$(lookup "$svc" service)
  aws ecs describe-task-definition \
    --task-definition "$family" \
    --region "$REGION" \
    --query 'taskDefinition.containerDefinitions[0].image' \
    --output text 2>/dev/null | sed 's/.*://' || true
}

# CDK 는 배포 대상이 의존하는 스택을 함께 갱신한다. 그래서 대상 서비스의 태그만
# 주입하면 함께 갱신되는 다른 서비스의 태스크 정의가 다른 태그로 바뀐다. 설정에
# 기본 태그가 없어 synth 가 네 태그를 모두 요구하므로, 배포하지 않는 서비스는 지금
# 떠 있는 태그를 그대로 넘겨 태스크 정의를 건드리지 않는다.
build_tag_env() {
  local target_svc=$1
  local svc tagenv tag
  for svc in $ALL_SERVICES; do
    tagenv=$(lookup "$svc" tagenv)
    if [[ "$svc" == "$target_svc" ]]; then
      tag="$IMAGE_TAG"
    else
      tag=$(deployed_tag "$svc")
      # 아직 배포되지 않은 서비스는 조회할 태스크 정의가 없다. 이 배포로 태스크
      # 정의가 처음 만들어지는 경우이므로 이번 태그를 쓴다.
      [[ -z "$tag" || "$tag" == "None" ]] && tag="$IMAGE_TAG"
    fi
    printf '%s=%s\n' "$tagenv" "$tag"
  done
}

do_ecs_deploy() {
  local svc=$1
  local stack
  stack=$(lookup "$svc" stack)
  local -a tag_env=()
  while IFS= read -r pair; do tag_env+=("$pair"); done < <(build_tag_env "$svc")
  log "스택 배포: $stack ($(lookup "$svc" tagenv)=${IMAGE_TAG})"
  # 태스크 정의가 불변 태그를 직접 가리키도록 CDK 로 배포한다. force-new-deployment
  # 만으로는 태스크 정의의 이미지 참조가 갱신되지 않는다.
  (cd "$INFRA_DIR" && env "${tag_env[@]}" npx cdk deploy "$stack" --require-approval never)
  ok "배포 완료: $svc"
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
IMAGE_TAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=true; shift ;;
    --status)     SHOW_STATUS=true; shift ;;
    --tag)        IMAGE_TAG="${2:?--tag 값이 필요합니다}"; shift 2 ;;
    --list)
      echo "Available services:"
      for s in $ALL_SERVICES; do echo "  $s"; done
      exit 0
      ;;
    --help|-h)
      echo "Usage: $0 [options] <service> [service...]"
      echo ""
      echo "Options:"
      echo "  --skip-build   ECR 이미지 빌드 없이 기존 태그로 스택만 재배포"
      echo "  --tag <tag>    배포할 이미지 태그 (기본: 현재 커밋 SHA)"
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

if [[ -z "$IMAGE_TAG" ]]; then
  IMAGE_TAG=$(resolve_image_tag)
fi
log "이미지 태그: ${IMAGE_TAG}"
if [[ "$IMAGE_TAG" == *-dirty ]]; then
  err "커밋되지 않은 변경이 있습니다 — 배포된 하네스를 커밋으로 재현할 수 없습니다"
fi

if [[ "$SKIP_BUILD" == "false" ]]; then
  ecr_login
  # 여러 서비스가 같은 이미지를 공유할 수 있으므로(분석 워커와 실행 워커) 리포지토리
  # 단위로 한 번만 빌드·푸시한다. 두 번 푸시해도 결과는 같지만 빌드 시간이 두 배가 된다.
  built_repos=""
  for svc in "${SERVICES[@]}"; do
    repo=$(lookup "$svc" repo)
    case " ${built_repos} " in
      *" ${repo} "*)
        log "빌드 생략: $svc — ${repo} 는 이미 이 태그로 푸시했습니다"
        continue
        ;;
    esac
    do_build "$svc"
    do_push "$svc"
    built_repos="${built_repos} ${repo}"
  done
else
  log "--skip-build: 빌드 생략"
fi

for svc in "${SERVICES[@]}"; do
  do_ecs_deploy "$svc"
done
