# chulgeun-publisher

인스타그램 @chulgeun_munjang 캐러셀 자동 게시 저장소. 설정은 [SETUP.md](SETUP.md), 설계 결정은 [docs/ADR-001-instagram-publishing.md](docs/ADR-001-instagram-publishing.md)(게시 방식 원안)와 [docs/ADR-002-facebook-login.md](docs/ADR-002-facebook-login.md)(Facebook 로그인 방식으로 전환, 현행).

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
.github/workflows/                   publish(월·수·금 07:17 KST + 09:17 백업), token-check(매주 일 03:17, 게시 안 함), ci
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
