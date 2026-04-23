#!/usr/bin/env bash
set -euo pipefail

REPO="haandol/rca-agent"

echo "=== GitHub PAT 등록 (GitHub Secrets + AWS Secrets Manager) ==="
echo ""

read -rsp "GitHub Personal Access Token 입력: " TOKEN
echo ""

if [[ -z "$TOKEN" ]]; then
  echo "오류: 토큰이 비어있습니다."
  exit 1
fi

REGION="${AWS_REGION:-us-east-1}"
NS="${NAMESPACE:-RcaAgentDev}"
SECRET_NAME="${NS}/github/pat"

echo ""
echo "[1/2] GitHub repo secret 등록 (${REPO})..."
echo "$TOKEN" | gh secret set GITHUB_PERSONAL_ACCESS_TOKEN -R "$REPO"
echo "  완료"

echo ""
echo "[2/2] AWS Secrets Manager 등록 (${SECRET_NAME}, ${REGION})..."
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value \
    --secret-id "$SECRET_NAME" \
    --secret-string "$TOKEN" \
    --region "$REGION"
  echo "  기존 시크릿 업데이트 완료"
else
  aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --description "GitHub Personal Access Token for RCA Agent and CC Headless" \
    --secret-string "$TOKEN" \
    --region "$REGION"
  echo "  새 시크릿 생성 완료"
fi

echo ""
echo "=== 완료 ==="
echo "GitHub secrets:"
gh secret list -R "$REPO"
echo ""
echo "AWS Secrets Manager: ${SECRET_NAME} (${REGION})"
