#!/bin/bash
# 맥(launchd)에서 인스타 게시 워크플로를 dispatch 하는 예약 트리거. ADR-003 참고.
#
# 호출: launchd(ops/launchd/com.hyuk.chulgeun.publish-trigger.plist)
#   - 월·수·금 07:17, 07:47 KST(StartCalendarInterval, 잠자기 중이었으면 깨어난 뒤 한 번)
#   - 로그인·설치 시 한 번(RunAtLoad, 재부팅·정전으로 놓친 시각 따라잡기)
# 어느 경우든 원격 main 큐에 '지금 올릴 항목'이 있을 때만 dispatch 한다(scripts/remote_due.py).
# 판정이 실패하면(네트워크 등) 재시도 후에도 dispatch 한다: 게시기 쪽 중복 방지가 있어 안전하고,
# 판정 실패로 게시를 놓치는 것보다 낫다.
#
# 옵션
#   --dry-run   dispatch 할 때 dry_run=true (인스타에 올리지 않음). 시험용
#   --force     판정 없이 dispatch. 시험용(보통 --dry-run 과 함께)
#   --no-wait   dispatch 후 실행 결과를 기다리지 않음
#
# 로그: $TRIGGER_LOG_DIR(기본 /Users/hyuk/project/.cache)/chulgeun-trigger.log
# 종료 코드: 0 정상(할 일 없음 포함), 1 dispatch 실패 또는 실행 실패

set -uo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="${LANG:-ko_KR.UTF-8}"
export GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1

REPO="hyuk0309/chulgeun-publisher"
WORKFLOW="publish.yml"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${TRIGGER_LOG_DIR:-/Users/hyuk/project/.cache}"
LOG="$LOG_DIR/chulgeun-trigger.log"
LOCK="$LOG_DIR/chulgeun-trigger.lock"
PY="/usr/bin/python3"

DRY_RUN=false FORCE=false WAIT=true
for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=true ;;
        --force) FORCE=true ;;
        --no-wait) WAIT=false ;;
        *) echo "알 수 없는 옵션: $a" >&2; exit 2 ;;
    esac
done

mkdir -p "$LOG_DIR"
# 로그가 1MB 넘으면 한 번 돌린다
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 1048576 ]; then mv -f "$LOG" "$LOG.1"; fi
exec >>"$LOG" 2>&1
log() { echo "[$(date '+%F %T %Z')] $*"; }

# 같은 시각에 두 번 겹쳐 돌지 않게(60분 넘은 잠금은 버림)
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
        log "오래된 잠금 제거: $LOCK"; rm -rf "$LOCK"; mkdir "$LOCK" || exit 1
    else
        log "이미 다른 트리거가 실행 중 → 종료"; exit 0
    fi
fi
trap 'rm -rf "$LOCK"' EXIT

log "시작 (dry_run=$DRY_RUN force=$FORCE, pid $$, ppid $PPID)"

if ! command -v gh >/dev/null; then log "실패: gh 명령을 찾을 수 없습니다(PATH=$PATH)"; exit 1; fi

# 1) 판정: 원격 main 기준. 부팅 직후 네트워크가 늦게 올라올 수 있어 최대 5번(30초 간격)
if [ "$FORCE" = true ]; then
    log "판정 건너뜀(--force)"
else
    verdict="" code=2
    for i in 1 2 3 4 5; do
        verdict="$("$PY" "$HERE/remote_due.py" --repo "$REPO" 2>"$LOG_DIR/.chulgeun-trigger.judge.err")"
        code=$?
        sed 's/^/    /' "$LOG_DIR/.chulgeun-trigger.judge.err"
        [ "$code" -ne 2 ] && break
        log "판정 실패($i/5), 30초 뒤 재시도"
        sleep 30
    done
    rm -f "$LOG_DIR/.chulgeun-trigger.judge.err"
    case "$code" in
        0) log "올릴 항목 있음: $verdict → dispatch" ;;
        3) log "할 일 없음(원격 main 기준) → dispatch 안 함"; exit 0 ;;
        *) log "경고: 판정 5번 실패(종료 $code) → 안전하게 dispatch(게시기가 중복·예약 전 항목을 걸러냄)" ;;
    esac
fi

# 2) dispatch: 최대 3번(30초·60초 간격)
before="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
out="" ok=false
for i in 1 2 3; do
    if out="$(gh workflow run "$WORKFLOW" --repo "$REPO" --ref main -f action=run -f dry_run="$DRY_RUN" 2>&1)"; then
        ok=true; break
    fi
    log "dispatch 실패($i/3): $out"
    [ "$i" -lt 3 ] && sleep $((i * 30))
done
if [ "$ok" != true ]; then
    log "실패: dispatch 3번 모두 실패. GitHub schedule 백업(09:17 KST)이 남아 있음. 원인: $out"
    exit 1
fi
log "dispatch 완료(dry_run=$DRY_RUN): $(echo "$out" | tr '\n' ' ')"

# 3) 실행 결과 기록(최대 30분). 실패해도 GitHub 쪽 '실패 알림 이슈'는 워크플로가 따로 만든다
run_url="$(echo "$out" | grep -Eo 'https://github.com/[^ ]+/actions/runs/[0-9]+' | head -1)"
if [ -z "$run_url" ]; then
    sleep 10
    run_url="$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --event workflow_dispatch -L 5 \
        --json url,createdAt --jq "[.[] | select(.createdAt >= \"$before\")][-1].url // empty" 2>/dev/null)"
fi
if [ -z "$run_url" ]; then log "경고: 실행 링크를 찾지 못했습니다. GitHub Actions 화면에서 확인하세요"; exit 0; fi
log "실행: $run_url"
[ "$WAIT" = true ] || exit 0

run_id="${run_url##*/}"
for _ in $(seq 1 60); do
    sleep 30
    st="$(gh run view "$run_id" --repo "$REPO" --json status,conclusion --jq '.status + " " + .conclusion' 2>&1)" || {
        log "상태 조회 실패: $st"; continue; }
    case "$st" in
        "completed success") log "실행 성공: $run_url"; exit 0 ;;
        completed*) log "실패: 실행 결과 '${st#completed }' $run_url (워크플로가 실패 이슈를 엽니다)"; exit 1 ;;
    esac
done
log "경고: 30분 안에 실행이 끝나지 않았습니다: $run_url (마지막 상태: ${st:-?})"
exit 1
