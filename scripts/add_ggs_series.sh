#!/bin/sh
# 『총, 균, 쇠』 3편 연재를 큐에 등록(또는 PNG 가 생긴 뒤 다시 실행해 JPEG 갱신).
# 게시일은 잠정(2026-11-16 월 / 11-18 수 / 11-20 금, 07:17 KST). 게시 순서 확정 전까지 승인하지 않는다.
# publisher/ 폴더가 reading-ideation/ 안에 있다는 전제의 상대 경로.
set -e
cd "$(dirname "$0")/.."
SERIES=../content/guns-germs-steel-series.md
EXPORT=../design/export
python3 scripts/queue_tool.py add --series "$SERIES" --part 1 --images "$EXPORT/ggs-1" --slug 2026-11-16-ggs-1 --at "2026-11-16 07:17"
python3 scripts/queue_tool.py add --series "$SERIES" --part 2 --images "$EXPORT/ggs-2" --slug 2026-11-18-ggs-2 --at "2026-11-18 07:17"
python3 scripts/queue_tool.py add --series "$SERIES" --part 3 --images "$EXPORT/ggs-3" --slug 2026-11-20-ggs-3 --at "2026-11-20 07:17"
python3 scripts/queue_tool.py list
