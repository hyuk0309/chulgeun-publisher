# ADR-002 Instagram 로그인 방식 → Facebook 로그인 방식(페이지 토큰)으로 전환

- 상태: 채택 (2026-11-07). ADR-001의 "게시 경로"·"API 버전" 행과 토큰 갱신 관련 항목을 대체한다.
- 결정자: 대표님(전환 요청), 에이전트(구현 세부)
- 확인한 공식 문서(2026-11-07 열람)
  - Content Publishing: https://developers.facebook.com/docs/instagram-platform/content-publishing
  - Instagram API with Facebook Login, Get started / Overview: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/get-started
  - 사용 사례 맞춤 설정: https://developers.facebook.com/documentation/development/create-an-app/instagram-use-case.md
  - 앱 만들기: https://developers.facebook.com/documentation/development/create-an-app.md
  - 장기 토큰: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens/get-long-lived.md
  - 데이터 접근 만료: https://developers.facebook.com/documentation/facebook-login/auth-vs-data.md

## 배경

ADR-001의 Instagram 로그인 방식은 (1) 인스타 웹에서 테스터 초대 수락, (2) 앱 대시보드에서 인스타 로그인 팝업으로 계정 추가 — 두 번 모두 **인스타 웹 로그인**이 필요하다. 대표님은 PC 웹에서 @chulgeun_munjang 로그인이 끝내 안 됐다(비밀번호 재설정 후에도 "로그인 정보가 잘못되었습니다", 재설정 링크 만료). 모바일 앱 로그인은 정상이다. 따라서 인스타 웹 로그인이 한 번도 필요 없는 경로가 필요하다.

## 결정

| 항목 | 선택 | 버린 대안과 이유 |
|---|---|---|
| 게시 경로 | **Instagram API with Facebook Login**. 호스트 `graph.facebook.com`. 앱 사용 사례 "Instagram에서 메시지 및 콘텐츠 관리(Manage messaging and content on Instagram)" → **API setup with Facebook Login**. 개발 모드 유지, 앱 심사·비즈니스 인증 없음 | Instagram 로그인 방식(ADR-001): 인스타 웹 로그인이 필수라 막힘. Buffer 등: 사람이 매주 올려야 함(대안으로 유지). 비공식 라이브러리: 약관 위반 소지 |
| 인스타-페이지 연결 | **인스타 모바일 앱**의 프로필 편집 → 페이지 → 연결. 그 과정의 로그인은 Facebook 로그인 | Facebook 페이지 설정 → 연결된 계정 → Instagram 연결: 웹에서 인스타 로그인 창이 떠서 배제. Meta Business Suite 연결: 마찬가지로 인스타 로그인 요구 가능 |
| 권한 | 토큰 발급 때 `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish`, `business_management` | 게시에 필수는 앞 4개(공식 Content Publishing 문서: instagram_basic, instagram_content_publish, pages_read_engagement / Get started: pages_show_list). `business_management` 는 사용 사례가 기본으로 붙이는 권한이고, 페이지가 비즈니스 포트폴리오에 들어가 있을 때 `/me/accounts` 가 비는 문제를 피하려고 함께 요청한다 |
| 토큰 | **장기 사용자 토큰에서 얻은 페이지 액세스 토큰**(만료 없음)을 GitHub Secret `FB_PAGE_ACCESS_TOKEN` 에 저장 | 장기 사용자 토큰(60일): 매주 갱신 워크플로와 Secret 쓰기용 PAT가 필요(ADR-001 방식). 시스템 사용자 토큰: 비즈니스 포트폴리오·관리 부담이 늘고, 익명 운영과 맞지 않음 |
| IG 사용자 ID | 페이지의 `instagram_business_account.id`. 설정 스크립트가 조회해 Secret `IG_USER_ID` 로 저장, 비어 있으면 실행 때 `GET /me?fields=instagram_business_account`(페이지 토큰의 /me 는 페이지)로 조회 | 수동 입력: 오타 위험 |
| 토큰 전달 | 대표님 맥에서 `scripts/setup_token.py` 1회 실행. 앱 ID(보임)·앱 시크릿·단기 토큰(getpass, 가림) 입력 → 교환·조회·검증 → `gh secret set` 의 **표준 입력**으로만 저장. 값은 출력하지 않음. gh 미설치·미로그인이면 안내 후 종료 | 대시보드·Secret 화면에 복사·붙여넣기: 채팅·메모장에 남을 위험. 에이전트가 대신 실행: 앱 시크릿과 토큰이 에이전트 대화로 들어옴 |
| 갱신 워크플로 | **없앰.** 대신 `token-check`(매주 일 03:17 KST): `publish.py check` 로 토큰 유효·페이지-인스타 연결·권한·`debug_token` 만료값을 확인하고, 문제 있으면 이슈. 게시하지 않음. PAT 불필요 | 매주 갱신 유지: 페이지 토큰에는 갱신 개념이 없음 |
| API 버전 | 기본 `v27.0`(공식 문서 최신), 저장소 변수 `GRAPH_API_VERSION` 으로 교체 | |

### 왜 페이지 토큰인가(근거)

- 공식 문서: "Long-lived Page access token do not have an expiration date and only expire or are invalidated under certain conditions." (get-long-lived 문서). 장기 사용자 토큰으로 `GET /{user-id}/accounts`(=`/me/accounts`)를 부르면 각 페이지의 장기 페이지 토큰이 나온다.
- 공식 문서(Overview): Facebook 로그인 경로에서는 "Facebook User or Page access tokens" 를 쓴다 — 페이지 토큰으로 IG 게시 API를 부를 수 있다.
- 무효화 조건(공식 문서에 명시된 것): 사용자가 비밀번호를 바꾸거나, 앱 권한을 해제하거나, 보안 사유. 추가로 페이지 역할이 사라지면 무효가 될 수 있다. → 이때만 대표님이 setup_token.py 를 다시 실행(약 5분).
- 결과: 60일마다 사람이 손대거나, 매주 Secret을 덮어쓰는 자동화(PAT 포함)가 필요 없다. 움직이는 부품과 1년짜리 PAT 만료 관리가 사라진다.

## 유지한 안전장치(ADR-001과 동일)

승인 게이트(`approved`), 중복 방지 3중(posted.json / 계정 최근 게시물 캡션 비교 / 게시 응답 끊김 시 재확인), POST 자동 재시도 없음, 컨테이너 FINISHED 확인 후 게시, 1회 1건, 20시간 넘게 밀린 항목 자동 게시 안 함, 이미지 URL HEAD 선확인, dry-run 은 Meta API를 부르지 않음, `concurrency` 로 동시 실행 1개. 토큰·앱 시크릿은 로그·예외에서 가린다(redact) — 이번에 앱 시크릿과 중간 토큰까지 가리도록 확장했다.

## 결과와 감수하는 점

- **운영용 Facebook 계정(실명)과 Facebook 페이지가 하나 늘어난다.** 페이지는 최소 정보(이름·카테고리만, 게시물 없음)로 둔다. 새 페이지 환경에서는 페이지를 완전히 비공개로 만드는 설정이 없을 수 있다 → 페이지 자체는 공개라고 가정한다. 페이지 이름은 채널명(출근문장)으로 두고, 관리자 개인 이름은 페이지에 표시되지 않게 한다.
- 인스타 프로필에 연결된 페이지가 표시될 수 있다. 인스타 앱의 프로필 표시 설정에서 숨길 수 있으면 숨긴다(SETUP.md 3단계).
- 운영용 Facebook 계정의 **비밀번호를 바꾸면 토큰이 무효**가 된다. 바꾼 뒤에는 setup_token.py 재실행이 필요하다(SETUP.md에 명시).
- 페이지 게시 승인(Page Publishing Authorization)이 요구되는 페이지는 PPA를 끝내기 전까지 게시가 막힌다(공식 문서). 새 소규모 페이지에서 요구될 가능성은 낮다고 보지만, 오류가 나면 페이지 알림에서 안내대로 진행한다.
- 24시간 게시 한도는 공식 문서상 50건(이전 100건 안내에서 변경). 주 3건 운영에는 영향 없음. 코드는 API가 돌려주는 값을 그대로 쓴다.
- 데이터 접근 만료(90일, 아래 미확인 2) 때문에 토큰이 유효해도 인스타 데이터 접근이 끊길 가능성이 있다 → 주간 점검이 14일 전에 이슈로 알린다(`debug_token` 이 값을 줄 때). 실제로 끊기면 setup_token.py 재실행으로 복구된다.

## 미확인(첫 실행으로 확인)

1. **개발 모드에서 앱 관리자 본인(운영용 Facebook 계정)으로 게시가 되는지.** 공식 문서상 Standard Access 는 "본인 소유·관리 계정만 쓰는 앱"에 맞고, 앱 심사는 "앱 역할이 없는 사용자"에게만 필요하다고 되어 있어 될 것으로 판단하지만, 개발 모드 + 게시를 직접 명시한 문장은 찾지 못했다. → SETUP.md 10단계 첫 실제 게시로 확인.
2. **데이터 접근 만료(90일)가 페이지 토큰의 인스타 권한에 적용되는지.** 공식 문서의 "만료되지 않는 권한" 목록에 `pages_*`, `business_management` 는 있으나 `instagram_basic`, `instagram_content_publish` 는 없다. → setup_token.py 와 주간 점검이 `debug_token` 의 `data_access_expires_at` 을 출력하므로 첫 실행 때 값(0이면 해당 없음)을 확인한다.
3. **페이지 토큰으로 `debug_token` 을 부를 수 있는지.** 문서는 access_token 으로 앱 토큰 또는 같은 앱의 토큰을 요구한다. 설정 스크립트는 앱 토큰(앱ID|앱시크릿, 저장 안 함)으로 확실히 확인하고, 주간 점검은 페이지 토큰으로 시도해 안 되면 건너뛴다(핵심 점검은 `/me` 와 게시 한도 조회로 이미 함).
4. 사용 사례 문서는 권한 이름을 `instagram_content_publishing` 으로 적은 곳이 있다. 권한 참조·Content Publishing 문서의 이름은 `instagram_content_publish` 이므로 이를 따른다. 화면에 다른 이름이 보이면 화면을 따른다.
5. 캐러셀 상한: 공식 문서 20장, 코드는 10장 제한 유지(ADR-001과 같음).
