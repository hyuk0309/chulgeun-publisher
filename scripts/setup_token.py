#!/usr/bin/env python3
"""한 번만 실행: Graph API Explorer 의 단기 토큰 → 만료 없는 페이지 토큰 → GitHub Secret 저장.

대표님이 맥 터미널에서 직접 실행한다. 토큰·앱 시크릿은 화면에 보이지 않게 입력받고,
어디에도 출력·저장하지 않으며 `gh secret set` 의 표준 입력으로만 넘긴다.

  python3 scripts/setup_token.py                      # 저장소는 현재 폴더의 GitHub 원격에서 자동 확인
  python3 scripts/setup_token.py --repo 계정/chulgeun-publisher

하는 일
  1) gh 설치·로그인 확인(안 돼 있으면 안내하고 종료)
  2) 앱 ID(보임), 앱 시크릿(가림), 단기 사용자 토큰(가림) 입력
  3) 단기 → 장기 사용자 토큰 교환(GET /oauth/access_token)
  4) /me/accounts 로 페이지와 연결된 IG 계정 조회 → 페이지 토큰 선택
  5) debug_token 으로 페이지 토큰이 '만료 없음'인지, 게시 권한이 있는지 확인
  6) 확인 질문 뒤 Secret 저장: FB_PAGE_ACCESS_TOKEN, IG_USER_ID

종료 코드: 0 저장 완료, 1 Meta 쪽 문제, 2 준비 안 됨(gh 등)·사용자가 취소
"""
from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ig_api  # noqa: E402
from ig_api import ApiError  # noqa: E402

SECRET_TOKEN = "FB_PAGE_ACCESS_TOKEN"
SECRET_UID = "IG_USER_ID"
EXPECTED_USERNAME = "chulgeun_munjang"


class Abort(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# gh 실행기: (args, stdin) -> (returncode, stdout, stderr). 테스트에서 가짜로 바꾼다.
Runner = Callable[[Sequence[str], Optional[str]], tuple]


def real_runner(args: Sequence[str], stdin: Optional[str] = None) -> tuple:
    p = subprocess.run(list(args), input=stdin, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def check_gh(run: Runner, which=shutil.which) -> str:
    """gh 설치·로그인 확인. 로그인된 GitHub 계정 이름을 돌려준다."""
    if not which("gh"):
        raise Abort("gh(GitHub CLI)가 설치돼 있지 않습니다.\n"
                    "  → 터미널에서 `brew install gh` 를 실행하거나 https://cli.github.com 에서 설치한 뒤 다시 실행하세요.")
    rc, _, _ = run(["gh", "auth", "status"], None)
    if rc != 0:
        raise Abort("gh 가 GitHub 에 로그인돼 있지 않습니다.\n"
                    "  → 터미널에서 `gh auth login` 을 실행해 **운영용 GitHub 계정**으로 로그인한 뒤 다시 실행하세요(SETUP.md 6단계).")
    rc, out, _ = run(["gh", "api", "user", "--jq", ".login"], None)
    return out.strip() if rc == 0 else "?"


def resolve_repo(run: Runner, repo: Optional[str], ask: Callable[[str], str]) -> str:
    if repo:
        return repo.strip()
    rc, out, _ = run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], None)
    if rc == 0 and out.strip():
        return out.strip()
    typed = ask("저장소 이름을 입력하세요(예: chulgeun-ops/chulgeun-publisher): ").strip()
    if "/" not in typed:
        raise Abort("저장소 이름은 '계정/저장소' 형식이어야 합니다.")
    return typed


def choose_page(pages: List[dict], ask: Callable[[str], str], out: Callable[[str], None]) -> dict:
    linked = [p for p in pages if (p.get("instagram_business_account") or {}).get("id")]
    if not pages:
        raise Abort("이 Facebook 계정으로 관리하는 페이지가 보이지 않습니다.\n"
                    "  → Graph API Explorer 에서 토큰을 만들 때 Facebook 로그인 창의 페이지 선택에서 해당 페이지를 체크했는지,\n"
                    "    권한에 pages_show_list, business_management 가 들어갔는지 확인하고 새 토큰으로 다시 실행하세요(SETUP.md 9단계).", 1)
    if not linked:
        names = ", ".join(p.get("name", "?") for p in pages)
        raise Abort(f"페이지({names})는 보이지만 인스타 계정이 연결된 페이지가 없습니다(또는 토큰에 인스타 권한이 없습니다).\n"
                    "  → 인스타 앱에서 페이지 연결(SETUP.md 3단계)을 확인하고, Explorer 로그인 창에서 인스타 계정도 체크한 뒤\n"
                    "    새 토큰으로 다시 실행하세요.", 1)
    if len(linked) == 1:
        return linked[0]
    out("인스타가 연결된 페이지가 여러 개입니다:")
    for i, p in enumerate(linked, 1):
        out(f"  {i}) {p.get('name', '?')} → @{p['instagram_business_account'].get('username', '?')}")
    typed = ask("번호를 고르세요: ").strip()
    if not typed.isdigit() or not 1 <= int(typed) <= len(linked):
        raise Abort("번호가 올바르지 않아 중단합니다.")
    return linked[int(typed) - 1]


def gh_secret_set(run: Runner, repo: str, name: str, value: str) -> None:
    # 값은 명령줄 인자가 아니라 표준 입력으로만 넘긴다(프로세스 목록·셸 기록에 남지 않게)
    rc, _, err = run(["gh", "secret", "set", name, "--repo", repo], value)
    if rc != 0:
        msg = (err or "").replace(value, "***").strip()
        raise Abort(f"GitHub Secret {name} 저장 실패: {msg[:300]}\n"
                    "  → 저장소 이름과, gh 로그인 계정이 그 저장소의 관리자(owner)인지 확인하세요.", 1)


def run_setup(args, *, ask: Callable[[str], str] = input,
              ask_secret: Callable[[str], str] = getpass.getpass,
              out: Callable[[str], None] = print, run: Runner = real_runner,
              which=shutil.which, transport=ig_api.urllib_transport,
              sleep=time.sleep, now_ts: Optional[int] = None) -> int:
    version = args.version or ig_api.DEFAULT_VERSION
    secrets_seen: List[str] = []

    def safe(text: str) -> str:
        for s in secrets_seen:
            if s:
                text = text.replace(s, "***")
        return text

    try:
        login = check_gh(run, which)
        repo = resolve_repo(run, args.repo, ask)
        out(f"GitHub 로그인 계정: {login}")
        out(f"저장할 저장소: {repo}")
        out("")
        out("아래 세 값을 붙여 넣으세요. 앱 시크릿과 토큰은 입력해도 화면에 보이지 않습니다(정상).")
        app_id = ask("앱 ID(숫자): ").strip()
        if not app_id.isdigit():
            raise Abort("앱 ID 는 숫자여야 합니다(앱 대시보드 → 앱 설정 → 기본 설정).")
        app_secret = ask_secret("앱 시크릿(보이지 않음): ").strip()
        short_token = ask_secret("Graph API Explorer 의 액세스 토큰(보이지 않음): ").strip()
        secrets_seen.extend([app_secret, short_token])
        if not app_secret or not short_token:
            raise Abort("앱 시크릿 또는 토큰이 비어 있습니다.")

        out("\n1/4 장기 사용자 토큰으로 교환 중…")
        try:
            body = ig_api.exchange_long_lived_user_token(short_token, app_id, app_secret,
                                                         transport=transport, version=version, sleep=sleep)
        except ApiError as e:
            raise Abort(f"교환 실패: {e}\n"
                        "  → 앱 ID·앱 시크릿이 이 앱의 것인지, Explorer 에서 같은 앱을 고르고 토큰을 만들었는지,\n"
                        "    토큰을 만든 지 1시간이 넘지 않았는지 확인하세요.", 1)
        user_token = body.get("access_token") or ""
        secrets_seen.append(user_token)
        if not user_token:
            raise Abort("교환 응답에 토큰이 없습니다.", 1)
        out(f"    완료(장기 사용자 토큰, 약 {int(body.get('expires_in') or 0) // 86400}일). 이 토큰은 저장하지 않습니다.")

        out("2/4 페이지와 연결된 인스타 계정 조회 중…")
        pages = ig_api.list_pages(user_token, transport=transport, version=version, sleep=sleep)
        for p in pages:
            secrets_seen.append(p.get("access_token") or "")
        page = choose_page(pages, ask, out)
        page_token = page.get("access_token") or ""
        ig = page["instagram_business_account"]
        if not page_token:
            raise Abort("페이지 토큰을 받지 못했습니다. 페이지 관리 권한(전체 권한)이 있는지 확인하세요.", 1)
        out(f"    페이지: {page.get('name', '?')} → 인스타 @{ig.get('username', '?')} (IG 사용자 ID {ig['id']})")
        if "CREATE_CONTENT" not in (page.get("tasks") or ["CREATE_CONTENT"]):
            out("    경고: 이 페이지에서 '콘텐츠 만들기' 권한이 보이지 않습니다. 게시가 거부되면 페이지 권한을 확인하세요.")

        out("3/4 페이지 토큰 확인 중…")
        checker = ig_api.GraphClient(ig_api.app_access_token(app_id, app_secret), transport=transport,
                                     version=version, sleep=sleep, secrets=[app_secret, page_token])
        try:
            info = checker.debug_token(page_token)
        except ApiError as e:
            raise Abort(f"토큰 확인 실패: {e}", 1)
        problems, notes = ig_api.token_health(info, int(now_ts if now_ts is not None else time.time()))
        for n in notes:
            out(f"    {n}")
        if problems:
            raise Abort("페이지 토큰에 문제가 있어 저장하지 않습니다:\n" +
                        "\n".join(f"  - {p}" for p in problems) +
                        "\n  → Explorer 에서 권한 5개를 모두 넣고 로그인 창에서 모두 허용했는지 확인한 뒤 새 토큰으로 다시 실행하세요.", 1)

        if ig.get("username") and ig["username"] != EXPECTED_USERNAME:
            out(f"    주의: 연결된 인스타 계정이 @{EXPECTED_USERNAME} 가 아니라 @{ig['username']} 입니다.")
        yn = ask(f"\n4/4 @{ig.get('username', '?')} 계정용 토큰을 {repo} 의 Secret 에 저장할까요? (y/N): ").strip().lower()
        if yn not in ("y", "yes", "ㅛ"):
            raise Abort("저장하지 않고 끝냈습니다.")
        gh_secret_set(run, repo, SECRET_TOKEN, page_token)
        gh_secret_set(run, repo, SECRET_UID, str(ig["id"]))
        out(f"\n저장 완료: {SECRET_TOKEN}, {SECRET_UID} (값은 화면에 출력하지 않았습니다)")
        out("다음: 저장소 Actions → '인스타 게시' → Run workflow → action: check (SETUP.md 10단계)")
        return 0
    except Abort as e:
        out(safe(f"\n중단: {e}"))
        return e.code
    except ApiError as e:
        out(safe(f"\n중단(Meta 응답 오류): {e}"))
        return 1
    except (KeyboardInterrupt, EOFError):
        out("\n취소했습니다. 아무것도 저장하지 않았습니다.")
        return 2


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="페이지 토큰을 만들어 GitHub Secret 에 저장(1회용)")
    p.add_argument("--repo", help="계정/저장소(생략하면 현재 폴더의 GitHub 원격)")
    p.add_argument("--version", help=f"Graph API 버전(기본 {ig_api.DEFAULT_VERSION})")
    return run_setup(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
