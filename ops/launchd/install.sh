#!/bin/bash
# 맥 예약 트리거 설치(또는 재설치). ADR-003.
#   ops/launchd/install.sh              설치: 템플릿을 채워 ~/Library/LaunchAgents 에 복사 → launchctl bootstrap
#   ops/launchd/install.sh --uninstall  해제: launchctl bootout → plist 삭제
# 설치하면 RunAtLoad 로 한 번 바로 돈다(할 일이 없으면 dispatch 하지 않음).
# 로그 폴더는 TRIGGER_LOG_DIR(기본 /Users/hyuk/project/.cache).
set -euo pipefail

LABEL="com.hyuk.chulgeun.publish-trigger"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$HERE/../.." && pwd)"
LOG_DIR="${TRIGGER_LOG_DIR:-/Users/hyuk/project/.cache}"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" || true
    echo "기존 에이전트 내림: $LABEL"
fi

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$DEST"
    echo "해제 완료: $DEST 삭제(로그는 $LOG_DIR/chulgeun-trigger*.log 에 남음)"
    exit 0
fi

mkdir -p "$LOG_DIR" "$(dirname "$DEST")"
chmod +x "$REPO_DIR/scripts/mac_trigger.sh"
sed -e "s#__REPO__#$REPO_DIR#g" -e "s#__LOG_DIR__#$LOG_DIR#g" "$HERE/$LABEL.plist" > "$DEST.tmp"
plutil -lint "$DEST.tmp" >/dev/null
mv -f "$DEST.tmp" "$DEST"
launchctl bootstrap "$DOMAIN" "$DEST"
echo "설치 완료: $DEST"
echo "  시각: 월·수·금 07:17, 07:47 (맥 시간대 $(readlink /etc/localtime | sed 's#.*/zoneinfo/##')) + 로그인 시 1회"
echo "  로그: $LOG_DIR/chulgeun-trigger.log"
echo "  상태: launchctl print $DOMAIN/$LABEL | grep -E 'state|last exit'"
