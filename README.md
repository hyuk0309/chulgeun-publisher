# chulgeun-publisher

인스타그램 @chulgeun_munjang 캐러셀 자동 게시 저장소. 설정은 [SETUP.md](SETUP.md), 설계 결정은 [docs/ADR-001-instagram-publishing.md](docs/ADR-001-instagram-publishing.md)(게시 방식 원안), [docs/ADR-002-facebook-login.md](docs/ADR-002-facebook-login.md)(Facebook 로그인 방식으로 전환, 현행), [docs/ADR-003-mac-launchd-trigger.md](docs/ADR-003-mac-launchd-trigger.md)(예약 실행을 맥 launchd에서 건다).

> 이 저장소는 공개입니다. `queue/` 의 캡션과 이미지는 인스타 게시 전부터 공개됩니다.

## 구조

```
queue/<YYYY-MM-DD-slug>/post.json    게시 시각(KST), 승인 여부, 캡션, 이미지별 alt_text
queue/<YYYY-MM-DD-slug>/01.jpg ...   1080x1350 JPEG (GitHub Pages 로 공개)
queue/<YYYY-MM-DD-slug>/posted.json  게시 후 자동 기록(media_id, permalink). 있으면 다시 게시하지 않음
scripts/queue_tool.py                로컬: PNG→JPEG(sips) + 큐 항목 생성, approve, list, validate
scripts/publish.py                   게시(run / check), --dry-run
scripts/ig_api.py                    graph.facebook.com 최소 클라이언트(표준 라이브러리만, 페이지 토큰)
scripts/setup_token.py               1회용: 단기 토큰 → 만료 없는 페이지 토큰 → gh secret set (대표님 맥에서 실행)
scripts/mac_trigger.sh               맥 launchd 예약 트리거: 할 일 있을 때만 publish.yml dispatch
scripts/remote_due.py                원격 main 큐 기준 '지금 올릴 항목' 판정(게시 안 함)
ops/launchd/                         트리거 plist 원본 + install.sh
.github/workflows/                   publish(맥이 dispatch, schedule 07:17·09:17 KST는 백업), token-check(매주 일 03:17, 게시 안 함), ci
```

## 자주 쓰는 명령(로컬, Python 3.9+, 외부 패키지 없음)

```sh
python3 scripts/queue_tool.py add --series ../content/guns-germs-steel-series.md --part 1 \
    --images ../design/export/ggs-1 --slug 2026-11-16-ggs-1 --at "2026-11-16 07:17"
python3 scripts/queue_tool.py list
python3 scripts/queue_tool.py approve 2026-11-16-ggs-1     # 대표님 OK 후에만
python3 scripts/publish.py run --dry-run --now "2026-11-16 07:17"
python3 -m unittest discover -s tests -v
```

- 캡션·이미지·시각이 바뀌면 `add` 가 승인을 자동 해제한다.
- 커밋 작성자는 운영용 GitHub noreply 주소로(`git config user.email`, 저장소 로컬 설정). 개인 이메일로 커밋하지 않는다.

## 예약 실행(맥 launchd, ADR-003)

월·수·금 07:17, 07:47 KST와 로그인 시 한 번, 맥이 원격 main 큐를 확인해 올릴 항목이 있을 때만 `publish.yml` 을 dispatch 한다. GitHub schedule 은 맥이 꺼졌을 때의 백업이다.

```sh
ops/launchd/install.sh                 # 설치·재설치(바로 한 번 돎, 할 일 없으면 dispatch 안 함)
ops/launchd/install.sh --uninstall     # 해제
tail -f /Users/hyuk/project/.cache/chulgeun-trigger.log     # 로그
launchctl print gui/$(id -u)/com.hyuk.chulgeun.publish-trigger | grep -E 'state|last exit'
scripts/mac_trigger.sh --dry-run --force   # 시험: 판정 없이 dry_run=true 로 dispatch(인스타에 안 올림)
```
