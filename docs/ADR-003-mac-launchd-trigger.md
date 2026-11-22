# ADR-003 예약 실행을 맥(launchd)에서 건다

- 상태: **채택** (2026-11-22)
- 결정자: 대표님(트리거 위치, 알림 없음), 에이전트(구현 세부)
- 관련: ADR-001(게시 안전장치), ADR-002(페이지 토큰·token-check)

## 배경

GitHub Actions 예약 실행(schedule)이 4번 연속 제시각에 돌지 않았다.

| 날짜 | 07:17 예약 | 09:17 백업 | 결과 |
|---|---|---|---|
| 11/13(금) | 안 돎 | 약 2시간 20분 늦게 실행 | 11:40 게시 |
| 11/16(월) | 안 돎 | 안 돎 | 10:14 수동 게시 |
| 11/18(수) | 안 돎 | 약 3시간 26분 늦게 실행 | 12:43 게시 |
| 11/20(금) | 안 돎 | 안 돎 | 12:01 수동 게시 |

GitHub 문서도 schedule은 부하가 높으면 늦어지거나 빠질 수 있다고 적고 있다. 정시에 확실히 호출해 줄 바깥 시계가 필요하다.

## 결정

**대표님 맥이 시계 역할을 한다.** 맥은 서버처럼 상시 켜 둔다(pmset sleep 0, caffeinate, AC 전원).

| 항목 | 내용 |
|---|---|
| 에이전트 | `~/Library/LaunchAgents/com.hyuk.chulgeun.publish-trigger.plist` (원본: `ops/launchd/`, 설치: `ops/launchd/install.sh`) |
| 시각 | 월·수·금 **07:17, 07:47** (맥 로컬 시간대 Asia/Seoul, `StartCalendarInterval`). 잠자기 중이었으면 깨어난 뒤 밀린 실행이 한 번 돈다 |
| 따라잡기 | `RunAtLoad`: 로그인할 때(재부팅·정전 뒤 포함)와 설치할 때 한 번 |
| 할 일 판정 | `scripts/remote_due.py`: **원격 main** 의 큐를 `gh api`(git trees + contents)로 읽어, publish.py와 같은 `queue_lib.select` 기준(승인됨, posted.json 없음, skip 아님, 예약 10분 전부터 20시간 뒤까지)에 맞는 항목이 있을 때만 dispatch. 로컬 작업트리·로컬 git은 보지 않는다 |
| 호출 | `gh workflow run publish.yml --repo hyuk0309/chulgeun-publisher --ref main -f action=run -f dry_run=false` (gh는 hyuk0309 계정, keyring 로그인) |
| 판정 실패 | 30초 간격 5번 재시도 후에도 실패하면 **그래도 dispatch** 한다. 게시기가 예약 전·게시된 항목을 걸러내므로 안전하고, 판정 실패 때문에 게시를 놓치는 것보다 낫다 |
| 결과 기록 | dispatch 후 최대 30분간 실행 상태를 확인해 성공·실패와 실행 링크를 로그에 남긴다 |
| 로그 | `/Users/hyuk/project/.cache/chulgeun-trigger.log` (1MB 넘으면 `.1` 로 돌림), launchd 자체 출력은 `chulgeun-trigger.launchd.log` |
| GitHub schedule | **남겨 둔다**: 맥이 꺼졌을 때의 백업(07:17·09:17 KST). 게시 로직은 바꾸지 않는다 |

**여러 번 호출해도 한 번만 올라가는 근거(코드 확인):**
- slug 없이 `run` 이면 `queue_lib.select` 가 고른 항목만, 한 번에 1건(`--max 1`) 올린다.
- 중복 방지 3중: ① `posted.json` 있으면 건너뜀 ② 계정 최근 게시물 10개와 캡션 앞 80자 대조 ③ `media_publish` 응답 오류 시 실제 게시 여부 재확인.
- `concurrency: instagram-publish`(`cancel-in-progress: false`)로 실행이 겹치지 않는다. 단, 앞 실행 중에 dispatch된 실행은 그 시점 커밋을 checkout 하므로 ①은 못 보고 ②가 막는다.

## 버린 대안

| 대안 | 버린 이유 |
|---|---|
| cron-job.org → fine-grained PAT로 dispatch (11/20 초안) | 외부 계정·PAT 만료 관리가 늘어난다. 맥이 상시 켜져 있어 필요 없음 |
| 휴대폰 알림(ntfy·텔레그램·메일) + 09:00 `verify` 명령 | 대표님 결정으로 만들지 않음. 실패는 기존대로 GitHub 이슈 + 맥 로그 |
| GitHub schedule 시각 추가 | 같은 원인으로 빠진다 |
| LaunchDaemon(로그인 전 실행) | FileVault가 켜져 있어 재부팅 뒤 잠금 해제 전에는 어차피 돌 수 없고, gh 로그인(keyring)이 사용자 세션에 묶여 있다 |
| 로컬 git(fetch 후 작업트리) 기준 판정 | 대표님·에이전트가 작업 중인 작업트리를 건드릴 수 있다. API로 원격 main만 읽는다 |

## 남는 위험

- **재부팅·정전 뒤 로그인 전에는 돌지 않는다.** FileVault가 켜져 있고 자동 로그인이 없다. 정전 뒤 자동 재시동(`pmset autorestart`)도 꺼져 있다. 이때는 GitHub schedule 백업만 남는다(지연·누락 가능). 로그인하면 RunAtLoad가 20시간 안의 놓친 항목을 따라잡는다.
- 맥 네트워크·GitHub 장애가 길면 dispatch 3번이 모두 실패한다. 이 경우 로그에 원인이 남고 GitHub schedule 백업에 기댄다.
- gh 로그인 토큰이 풀리면(로그아웃·폐기) 판정·dispatch가 모두 실패한다. 로그에 `gh api ... 실패` 가 남는다.
- 실행 스크립트는 저장소 작업트리(`scripts/mac_trigger.sh`, `remote_due.py`, `queue_lib.py`)에서 바로 읽는다. 작업트리를 다른 브랜치로 바꾸거나 고치면 트리거 동작도 바뀐다.
- 알림이 없으므로 실패는 GitHub 이슈(이메일)나 맥 로그를 봐야 안다.
