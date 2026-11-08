#!/usr/bin/env bash
# 워크플로 실패 시 GitHub 이슈를 만들거나(열린 이슈가 있으면) 댓글을 단다.
# 사용: notify_failure.sh "<제목>"   (GH_TOKEN, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID 필요)
set -euo pipefail
TITLE="$1"
LABEL="publish-failure"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
BODY="자동 실행이 실패했습니다.

- 실행 기록: ${RUN_URL}
- 시각(UTC): $(date -u '+%Y-%m-%d %H:%M')

실행 기록을 열어 빨간 X 단계의 마지막 줄(\"확인 필요\" 또는 \"[실패]\")을 읽어 주세요.
SETUP.md 의 \"막힐 때\" 표에 자주 나오는 원인과 해결이 있습니다.
해결되면 이 이슈를 닫아 주세요."

gh label create "$LABEL" --color D93F0B --description "자동 게시·토큰 점검 실패" --force >/dev/null 2>&1 || true
EXISTING=$(gh issue list --label "$LABEL" --state open --search "\"$TITLE\" in:title" --json number --jq '.[0].number' || true)
if [ -n "${EXISTING}" ]; then
  gh issue comment "$EXISTING" --body "$BODY"
else
  gh issue create --title "$TITLE" --label "$LABEL" --body "$BODY"
fi
