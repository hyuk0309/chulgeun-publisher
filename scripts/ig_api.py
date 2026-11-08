"""Instagram API with Facebook Login (graph.facebook.com) 최소 클라이언트.

- 게시에는 Facebook 페이지 액세스 토큰(장기, 만료 없음)을 쓴다. ADR-002 참고.
- IG 사용자 ID 는 페이지의 instagram_business_account 에서 얻는다.
- 표준 라이브러리(urllib)만 사용한다.
- 토큰·앱 시크릿은 로그·예외 메시지에 절대 남기지 않는다(redact).
- HTTP 전송은 Transport 로 감싸 테스트에서 가짜로 바꿀 수 있다.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_HOST = "https://graph.facebook.com"
DEFAULT_VERSION = "v27.0"   # 2026-11-07 공식 문서 기준 최신. GRAPH_API_VERSION 으로 바꿀 수 있다.

# 게시에 필요한 권한(공식 문서: Content Publishing, Facebook Login 경로)
PUBLISH_SCOPES = ("instagram_basic", "instagram_content_publish", "pages_read_engagement")
# 토큰 발급 때 요청할 권한(위 + 페이지 목록 조회 + 사용 사례 기본 포함 권한)
REQUEST_SCOPES = ("pages_show_list", "pages_read_engagement", "instagram_basic",
                  "instagram_content_publish", "business_management")


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None,
                 code: Optional[int] = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.transient = transient


# (method, url, params) -> (http_status, parsed_json)
Transport = Callable[[str, str, Dict[str, str]], Tuple[int, dict]]


def urllib_transport(method: str, url: str, params: Dict[str, str],
                     timeout: float = 60.0) -> Tuple[int, dict]:
    data = None
    if method == "GET":
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            body = {}
        return e.code, body


def head_check(url: str, timeout: float = 30.0) -> Tuple[bool, str]:
    """공개 이미지 URL 이 열리는지 확인(Meta API 호출 아님)."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            if "jpeg" not in ctype and "jpg" not in ctype:
                return False, f"Content-Type {ctype!r}"
            return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # 네트워크 오류
        return False, f"{type(e).__name__}: {e}"


class GraphClient:
    """graph.facebook.com 공통 호출부. access_token 하나와 가릴 비밀값 목록을 가진다."""

    def __init__(self, access_token: str, transport: Transport = urllib_transport,
                 host: str = DEFAULT_HOST, version: str = DEFAULT_VERSION,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print,
                 get_retries: int = 2, secrets: Iterable[str] = ()):
        if not access_token:
            raise ApiError("액세스 토큰이 비어 있습니다")
        self._token = access_token
        self._secrets = [s for s in [access_token, *secrets] if s]
        self._transport = transport
        self._base = f"{host.rstrip('/')}/{version}" if version else host.rstrip("/")
        self._sleep = sleep
        self._log = log
        self._get_retries = get_retries

    def _redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def _call(self, method: str, path: str, params: Optional[Dict[str, str]] = None,
              with_token: bool = True) -> dict:
        url = f"{self._base}/{path.lstrip('/')}"
        p = dict(params or {})
        if with_token:
            p["access_token"] = self._token
        attempts = 1 + (self._get_retries if method == "GET" else 0)
        last: Optional[ApiError] = None
        for i in range(attempts):
            try:
                status, body = self._transport(method, url, p)
            except Exception as e:  # 네트워크 단절, 타임아웃
                last = ApiError(self._redact(f"{method} {path}: 네트워크 오류 {type(e).__name__}: {e}"),
                                transient=True)
            else:
                if 200 <= status < 300 and "error" not in body:
                    return body
                err = body.get("error", {}) if isinstance(body, dict) else {}
                msg = err.get("error_user_msg") or err.get("message") or json.dumps(body, ensure_ascii=False)[:300]
                code = err.get("code")
                transient = status >= 500 or bool(err.get("is_transient")) or code in (1, 2, 4, 17, 341)
                last = ApiError(self._redact(f"{method} {path}: HTTP {status} code={code} {msg}"),
                                status=status, code=code, transient=transient)
                if not transient:
                    raise last
            if i + 1 < attempts:
                self._sleep(5 * (i + 1))
        assert last is not None
        raise last

    def debug_token(self, input_token: Optional[str] = None) -> dict:
        """토큰 상세(type, is_valid, expires_at, data_access_expires_at, scopes).

        access_token 은 이 클라이언트의 토큰(앱 토큰 또는 같은 앱의 토큰)을 쓴다.
        """
        body = self._call("GET", "debug_token", {"input_token": input_token or self._token})
        return body.get("data") or {}


class InstagramClient(GraphClient):
    """페이지 액세스 토큰으로 연결된 IG 프로페셔널 계정에 게시한다."""

    # ------------------------------------------------------------ 계정

    def page_info(self) -> dict:
        """페이지 토큰의 /me 는 페이지 자신이다. 연결된 IG 계정까지 한 번에 조회."""
        return self._call("GET", "me", {"fields": "id,name,instagram_business_account{id,username}"})

    def ig_account(self, ig_user_id: str) -> dict:
        return self._call("GET", ig_user_id, {"fields": "id,username"})

    def publishing_limit(self, ig_user_id: str) -> dict:
        body = self._call("GET", f"{ig_user_id}/content_publishing_limit",
                          {"fields": "quota_usage,config"})
        data = body.get("data") or [{}]
        return data[0]

    def recent_media(self, ig_user_id: str, limit: int = 10) -> List[dict]:
        body = self._call("GET", f"{ig_user_id}/media",
                          {"fields": "id,caption,permalink,timestamp", "limit": str(limit)})
        return list(body.get("data") or [])

    # ------------------------------------------------------------ 게시

    def create_carousel_item(self, ig_user_id: str, image_url: str,
                             alt_text: Optional[str] = None) -> str:
        params = {"image_url": image_url, "is_carousel_item": "true"}
        if alt_text:
            params["alt_text"] = alt_text
        try:
            body = self._call("POST", f"{ig_user_id}/media", params)
        except ApiError as e:
            # alt_text 를 캐러셀 항목에서 받지 않는 경우에 대비해 한 번만 빼고 다시 시도
            if alt_text and e.code == 100 and "alt" in str(e).lower():
                self._log("  경고: alt_text 가 거부되어 alt_text 없이 다시 만듭니다")
                body = self._call("POST", f"{ig_user_id}/media",
                                  {"image_url": image_url, "is_carousel_item": "true"})
            else:
                raise
        return body["id"]

    def create_carousel(self, ig_user_id: str, children: List[str], caption: str) -> str:
        body = self._call("POST", f"{ig_user_id}/media", {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
        })
        return body["id"]

    def container_status(self, container_id: str) -> dict:
        return self._call("GET", container_id, {"fields": "status_code,status"})

    def wait_finished(self, container_id: str, interval: float = 15.0,
                      timeout: float = 300.0, first_wait: float = 3.0) -> None:
        """상태가 FINISHED 가 될 때까지 폴링(공식 권장: 5분 이내)."""
        waited = 0.0
        self._sleep(first_wait)
        waited += first_wait
        while True:
            st = self.container_status(container_id)
            code = st.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise ApiError(f"컨테이너 {container_id} 상태 {code}: {st.get('status', '')}")
            if code == "PUBLISHED":
                raise ApiError(f"컨테이너 {container_id} 가 이미 게시됨(PUBLISHED) — 중복 게시 방지를 위해 중단")
            if waited >= timeout:
                raise ApiError(f"컨테이너 {container_id} 가 {int(timeout)}초 안에 FINISHED 되지 않음(마지막 상태 {code})",
                               transient=True)
            self._sleep(interval)
            waited += interval

    def media_publish(self, ig_user_id: str, creation_id: str) -> str:
        body = self._call("POST", f"{ig_user_id}/media_publish", {"creation_id": creation_id})
        return body["id"]

    def media_info(self, media_id: str) -> dict:
        return self._call("GET", media_id, {"fields": "id,permalink,timestamp"})


# ---------------------------------------------------------------- 토큰 발급(setup_token.py 전용)

def exchange_long_lived_user_token(short_token: str, app_id: str, app_secret: str,
                                   transport: Transport = urllib_transport,
                                   version: str = DEFAULT_VERSION,
                                   sleep: Callable[[float], None] = time.sleep) -> dict:
    """단기 사용자 토큰 → 장기 사용자 토큰(약 60일). 응답: access_token, expires_in."""
    c = GraphClient(short_token, transport=transport, version=version, sleep=sleep,
                    secrets=[app_secret])
    return c._call("GET", "oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    }, with_token=False)


def list_pages(user_token: str, transport: Transport = urllib_transport,
               version: str = DEFAULT_VERSION,
               sleep: Callable[[float], None] = time.sleep) -> List[dict]:
    """장기 사용자 토큰으로 /me/accounts 조회. 각 항목의 access_token 이 만료 없는 페이지 토큰."""
    c = GraphClient(user_token, transport=transport, version=version, sleep=sleep)
    body = c._call("GET", "me/accounts", {
        "fields": "id,name,access_token,tasks,instagram_business_account{id,username}",
        "limit": "100",
    })
    return list(body.get("data") or [])


def app_access_token(app_id: str, app_secret: str) -> str:
    """앱 토큰(앱ID|앱시크릿). debug_token 의 access_token 으로만 쓴다. 저장하지 않는다."""
    return f"{app_id}|{app_secret}"


# ---------------------------------------------------------------- 토큰 상태 판정(순수 함수)

TOKEN_WARN_DAYS = 14


def token_health(info: dict, now_ts: int, warn_days: int = TOKEN_WARN_DAYS) -> Tuple[List[str], List[str]]:
    """debug_token 결과로 (문제 목록, 참고 목록)을 만든다. 문제가 있으면 사람이 손봐야 한다.

    - type 이 PAGE 가 아니면 문제(게시는 페이지 토큰 전제).
    - expires_at 이 0 이면 만료 없음. 0 이 아니고 warn_days 안이면 문제.
    - data_access_expires_at 이 0 이 아니고 warn_days 안이면 문제(권한 재허용 필요).
    - 게시 권한(PUBLISH_SCOPES)이 scopes 에 없으면 문제.
    """
    problems: List[str] = []
    notes: List[str] = []
    day = 86400
    if info.get("is_valid") is False:
        problems.append("토큰이 무효입니다(비밀번호 변경·앱 권한 해제·보안 조치 등)")
        return problems, notes
    ttype = info.get("type")
    if ttype and ttype != "PAGE":
        problems.append(f"토큰 종류가 {ttype} 입니다. 페이지 토큰이어야 합니다(setup_token.py 로 다시 저장)")
    exp = int(info.get("expires_at") or 0)
    if exp == 0:
        notes.append("토큰 만료: 없음")
    else:
        left = (exp - now_ts) // day
        (problems if exp - now_ts < warn_days * day else notes).append(f"토큰 만료까지 약 {left}일")
    dexp = int(info.get("data_access_expires_at") or 0)
    if dexp:
        left = (dexp - now_ts) // day
        (problems if dexp - now_ts < warn_days * day else notes).append(
            f"데이터 접근 권한 만료까지 약 {left}일")
    scopes = info.get("scopes")
    if scopes is not None:
        missing = [s for s in PUBLISH_SCOPES if s not in scopes]
        if missing:
            problems.append(f"게시에 필요한 권한이 빠졌습니다: {', '.join(missing)}")
        else:
            notes.append("게시 권한: 있음")
    return problems, notes
