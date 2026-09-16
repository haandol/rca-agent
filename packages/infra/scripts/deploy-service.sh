#!/usr/bin/env bash
set -euo pipefail

# Deploy a single ECS service: build Docker image → push to ECR → ECS force new deployment.
#
# Usage:
#   bash deploy-service.sh <service-name>
#   bash deploy-service.sh headless-codex
#   bash deploy-service.sh agent
#   bash deploy-service.sh healthcare
#   bash deploy-service.sh execution
#   bash deploy-service.sh headless-codex execution   # 같은 이미지, 두 진입점
#   bash deploy-service.sh --list
#   bash deploy-service.sh --skip-build headless-codex
#   bash deploy-service.sh --status headless-codex

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
    agent:container)     echo "rca-agent" ;;
    agent:repo)          echo "${ECR_NS}/rca-agent" ;;
    agent:cluster)       echo "${PREFIX}RcaAgent" ;;
    agent:service)       echo "${PREFIX}RcaAgent" ;;
    headless-codex:ctx)     echo "packages/headless-codex" ;;
    headless-codex:container) echo "cc-headless" ;;
    # The deployed repository and ECS service names stay stable for in-place replacement.
    headless-codex:repo)    echo "${ECR_NS}/cc-headless" ;;
    headless-codex:cluster) echo "${PREFIX}CcHeadless" ;;
    headless-codex:service) echo "${PREFIX}CcHeadless" ;;
    healthcare:ctx)      echo "packages/healthcare-sensor-app" ;;
    healthcare:container) echo "healthcare" ;;
    healthcare:repo)     echo "${ECR_NS}/healthcare" ;;
    healthcare:cluster)  echo "${PREFIX}Healthcare" ;;
    healthcare:service)  echo "${PREFIX}Healthcare" ;;
    # 실행 워커는 분석 워커와 같은 이미지를 다른 진입점으로 띄운다. 그래서 빌드
    # 컨텍스트와 리포지토리가 headless-codex 와 동일하고, 배포 대상 스택만 다르다.
    execution:ctx)       echo "packages/headless-codex" ;;
    execution:container) echo "playbook-execution" ;;
    execution:repo)      echo "${ECR_NS}/cc-headless" ;;
    execution:cluster)   echo "${PREFIX}PlaybookExecution" ;;
    execution:service)   echo "${PREFIX}PlaybookExecution" ;;
    agent:stack)         echo "${PREFIX}RcaAgentServiceStack" ;;
    agent:tagenv)        echo "AGENT_IMAGE_TAG" ;;
    headless-codex:stack)   echo "${PREFIX}CcHeadlessStack" ;;
    headless-codex:tagenv)  echo "HEADLESS_CODEX_IMAGE_TAG" ;;
    healthcare:stack)    echo "${PREFIX}HealthcareServiceStack" ;;
    healthcare:tagenv)   echo "HEALTHCARE_IMAGE_TAG" ;;
    execution:stack)     echo "${PREFIX}PlaybookExecutionStack" ;;
    execution:tagenv)    echo "EXECUTION_IMAGE_TAG" ;;
    *) echo "Unknown: ${svc}:${field}" >&2; return 1 ;;
  esac
}

ALL_SERVICES="agent headless-codex healthcare execution"

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

# Resolve the service's actual task definition, never the newest family revision.
service_task_definition() {
  aws ecs describe-services --cluster "$(lookup "$1" cluster)" \
    --services "$(lookup "$1" service)" --region "$REGION" \
    --query 'services[0].taskDefinition' --output text
}

# Preserve the unique app image tag from the service's selected task definition.
# Container order is not identity: tracing sidecars may precede the application.
deployed_tag() {
  local td definition repository container
  td=$(service_task_definition "$1") || return 1
  [[ -n "$td" && "$td" != "None" ]] || return 1
  repository="${ECR_REGISTRY}/$(lookup "$1" repo)" || return 1
  container=$(lookup "$1" container) || return 1
  definition=$(aws ecs describe-task-definition --task-definition "$td" --region "$REGION" \
    --output json) || return 1
  python3 - "$td" "$repository" "$container" "$definition" <<'PYCODE'
import json
import re
import sys

td, repository, name = sys.argv[1:4]
definition = json.loads(sys.argv[4]).get("taskDefinition", {})
if definition.get("taskDefinitionArn") != td:
    sys.exit("Preserved app task definition differs from the selected service revision")
containers = definition.get("containerDefinitions", [])
matches = [
    c for c in containers
    if isinstance(c.get("image"), str)
    and c["image"].split("@", 1)[0].split(":", 1)[0] == repository
]
if len(matches) != 1 or matches[0].get("name") != name:
    sys.exit("Preserved service requires one app container matching its repository and name")
image = matches[0]["image"]
prefix = repository + ":"
tag = image[len(prefix):] if image.startswith(prefix) else ""
if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
    sys.exit("Preserved app image has no explicit valid tag")
print(tag)
PYCODE
}

# Validate pins before any CDK call so missing metadata cannot fall back to tags.
require_digest() {
  [[ "$1" =~ ^sha256:[a-f0-9]{64}$ ]] || {
    err "Healthcare image digest is missing or invalid"; return 1;
  }
}

# Read the running service identity; reject rolling/mixed or unobserved baselines.
healthcare_current_identity() {
  local td definition tasks observed
  td=$(service_task_definition healthcare) || return 1
  [[ -n "$td" && "$td" != "None" ]] || return 1
  definition=$(aws ecs describe-task-definition --task-definition "$td" --region "$REGION" --output json) || return 1
  tasks=$(aws ecs list-tasks --cluster "$(lookup healthcare cluster)" \
    --service-name "$(lookup healthcare service)" --desired-status RUNNING \
    --region "$REGION" --query taskArns --output text) || return 1
  [[ -n "$tasks" && "$tasks" != "None" ]] || return 1
  local -a task_arns=()
  read -r -a task_arns <<< "$tasks"
  observed=$(aws ecs describe-tasks --cluster "$(lookup healthcare cluster)" \
    --tasks "${task_arns[@]}" --region "$REGION" --output json) || return 1
  python3 - "$td" "$definition" "$observed" <<'PYCODE'
import json
import re
import sys

td, definition, observed = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
containers = definition["taskDefinition"]["containerDefinitions"]
container = next(c for c in containers if c["name"] == "healthcare")
label = next(e["value"] for e in container["environment"] if e["name"] == "DEPLOYED_REVISION")
tasks = observed.get("tasks", [])
if observed.get("failures") or not tasks or any(
    t.get("taskDefinitionArn") != td or t.get("lastStatus") != "RUNNING" for t in tasks
):
    sys.exit("Healthcare has no consistent running service baseline")
digests = [c.get("imageDigest", "") for t in tasks for c in t["containers"] if c["name"] == "healthcare"]
if len(digests) != len(tasks) or len(set(digests)) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digests[0]):
    sys.exit("Healthcare running image digests are missing or inconsistent")
if "@" in container["image"] and container["image"].split("@", 1)[1] != digests[0]:
    sys.exit("Healthcare task definition and running digest disagree")
if not label or any(c.isspace() for c in label):
    sys.exit("Healthcare revision label is invalid")
print(label, digests[0])
PYCODE
}

# Pass the newly selected Healthcare tag's ECR digest, or the observed current
# Healthcare digest when deploying another service. Never inherit a stale env pin.
build_tag_env() {
  local target_svc=$1
  local svc tagenv tag digest identity
  if [[ "$target_svc" == "healthcare" ]]; then
    digest=$(aws ecr describe-images --repository-name "$(lookup healthcare repo)" \
      --image-ids "imageTag=$IMAGE_TAG" --region "$REGION" \
      --query 'imageDetails[0].imageDigest' --output text) || return 1
    require_digest "$digest" || return 1
    identity="$IMAGE_TAG $digest"
  else
    identity=$(healthcare_current_identity) || return 1
  fi
  for svc in $ALL_SERVICES; do
    tagenv=$(lookup "$svc" tagenv)
    if [[ "$svc" == "healthcare" ]]; then
      read -r tag digest <<< "$identity"
      require_digest "$digest" || return 1
      printf 'HEALTHCARE_IMAGE_DIGEST=%s\n' "$digest"
    elif [[ "$svc" == "$target_svc" ]]; then
      tag="$IMAGE_TAG"
    else
      tag=$(deployed_tag "$svc") || return 1
      [[ -n "$tag" && "$tag" != "None" ]] || return 1
    fi
    printf '%s=%s\n' "$tagenv" "$tag"
  done
}

assert_legacy_headless_queue_empty() {
  local queue_name="${PREFIX}CcHeadlessQueue"
  local queue_url counts visible in_flight delayed total
  queue_url=$(aws sqs get-queue-url \
    --queue-name "$queue_name" \
    --region "$REGION" \
    --query QueueUrl \
    --output text 2>/dev/null || true)
  [[ -z "$queue_url" || "$queue_url" == "None" ]] && return

  counts=$(aws sqs get-queue-attributes \
    --queue-url "$queue_url" \
    --region "$REGION" \
    --attribute-names \
      ApproximateNumberOfMessages \
      ApproximateNumberOfMessagesNotVisible \
      ApproximateNumberOfMessagesDelayed \
    --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible,ApproximateNumberOfMessagesDelayed]' \
    --output text)
  read -r visible in_flight delayed <<< "$counts"
  total=$((visible + in_flight + delayed))
  if (( total > 0 )); then
    err "레거시 Headless Codex 큐에 메시지 ${total}건이 남아 있어 공용 큐 전환을 중단합니다"
    err "큐를 비우거나 메시지를 공용 ${PREFIX}AlarmQueue로 옮긴 뒤 다시 배포하세요"
    exit 1
  fi
}

do_ecs_deploy() {
  local svc=$1
  local stack
  stack=$(lookup "$svc" stack)
  if [[ "$svc" == "headless-codex" ]]; then
    assert_legacy_headless_queue_empty
  fi
  local -a tag_env=()
  local resolved_env
  resolved_env=$(build_tag_env "$svc") || return 1
  while IFS= read -r pair; do tag_env+=("$pair"); done <<< "$resolved_env"
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
