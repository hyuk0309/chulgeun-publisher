"""테스트 공용: 가짜 Graph API(graph.facebook.com), 가짜 JPEG, 임시 큐."""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

TOKEN = "EAAtestPAGEsecretTOKEN123"          # 페이지 토큰(만료 없음)
UID = "17841400000000000"                   # IG 사용자 ID
PAGE_ID = "100000000000001"
PAGE_NAME = "출근문장"
APP_ID = "1234567890"
APP_SECRET = "appSECRETabcdef0123"
SHORT_TOKEN = "EAAshortUSERtoken999"
LONG_USER_TOKEN = "EAAlongUSERtoken888"


def fake_jpeg(width: int = 1080, height: int = 1350) -> bytes:
    """SOI + APP0 + SOF0 + EOI 로 된 최소 JPEG 헤더(크기 판독용)."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


CAPTION = ("결과가 안 좋은 주, \"내가 부족해서\"라고 결론 내리기 전에.\n본문입니다.\n\n"
           "#직장인 #직장인공감 #슬럼프 #총균쇠 #출근문장")


def make_item(queue_dir: Path, slug: str, scheduled_at: str, approved: bool = True,
              n_images: int = 3, caption: str = CAPTION, posted: Optional[dict] = None,
              write_images: bool = True, size=(1080, 1350), extra: Optional[dict] = None) -> Path:
    d = queue_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    images = []
    for i in range(1, n_images + 1):
        name = f"{i:02d}.jpg"
        if write_images:
            (d / name).write_bytes(fake_jpeg(*size))
        images.append({"file": name, "alt_text": f"{i}/{n_images} 카드"})
    data = {"schema": 1, "slug": slug, "title": slug, "scheduled_at": scheduled_at,
            "approved": approved, "caption": caption, "images": images}
    if extra:
        data.update(extra)
    (d / "post.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if posted:
        (d / "posted.json").write_text(json.dumps(posted), encoding="utf-8")
    return d


class FakeGraph:
    """graph.facebook.com 을 흉내 내는 가짜 transport."""

    def __init__(self):
        self.calls: List[tuple] = []
        self.recent: List[dict] = []
        self.status_seq: Dict[str, List[str]] = {}
        self.default_status = "FINISHED"
        self.fail: Dict[str, tuple] = {}         # 경로 키워드 -> (http_status, error dict)
        self.fail_times: Dict[str, int] = {}     # 실패를 몇 번 낼지(기본 무한)
        self.publish_side_effect_before_error = False
        self.reject_alt_text = False
        self._n = 0
        self.tokens_used: List[Optional[str]] = []
        self.published: List[str] = []
        self.last_caption = None
        # 토큰·페이지 상태
        self.ig_linked = True
        self.debug_info: Dict[str, dict] = {
            TOKEN: {"app_id": APP_ID, "type": "PAGE", "is_valid": True, "expires_at": 0,
                    "data_access_expires_at": 0, "profile_id": PAGE_ID,
                    "scopes": ["pages_show_list", "pages_read_engagement", "instagram_basic",
                               "instagram_content_publish", "business_management"]},
        }
        self.pages: Optional[List[dict]] = None   # None 이면 기본 페이지 1개

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def _maybe_fail(self, key):
        if key in self.fail:
            left = self.fail_times.get(key)
            if left is None or left > 0:
                if left is not None:
                    self.fail_times[key] = left - 1
                status, err = self.fail[key]
                return status, {"error": err}
        return None

    def __call__(self, method: str, url: str, params: Dict[str, str]):
        assert url.startswith("https://graph.facebook.com/"), url
        path = url.split("graph.facebook.com/", 1)[1]
        parts = path.split("/")
        if parts[0].startswith("v") and parts[0][1:2].isdigit():
            parts = parts[1:]
        path = "/".join(parts)
        tok = params.get("access_token")
        self.calls.append((method, path, {k: v for k, v in params.items() if k != "access_token"}))
        self.tokens_used.append(tok)

        # ---- 토큰 발급(setup_token.py)
        if method == "GET" and path == "oauth/access_token":
            assert tok is None, "교환 호출에 access_token 을 붙이면 안 됨"
            r = self._maybe_fail("exchange")
            if r:
                return r
            assert params["client_id"] == APP_ID and params["client_secret"] == APP_SECRET
            assert params["fb_exchange_token"] == SHORT_TOKEN
            return 200, {"access_token": LONG_USER_TOKEN, "token_type": "bearer", "expires_in": 5183944}
        if method == "GET" and path == "me/accounts":
            assert tok == LONG_USER_TOKEN
            if self.pages is not None:
                return 200, {"data": self.pages}
            page = {"id": PAGE_ID, "name": PAGE_NAME, "access_token": TOKEN,
                    "tasks": ["ANALYZE", "ADVERTISE", "MODERATE", "CREATE_CONTENT", "MANAGE"]}
            if self.ig_linked:
                page["instagram_business_account"] = {"id": UID, "username": "chulgeun_munjang"}
            return 200, {"data": [page]}
        if method == "GET" and path == "debug_token":
            r = self._maybe_fail("debug")
            if r:
                return r
            info = self.debug_info.get(params["input_token"])
            if info is None:
                return 400, {"error": {"message": "Invalid OAuth access token", "code": 190}}
            return 200, {"data": info}

        # ---- 이하 게시 경로는 페이지 토큰만 허용
        assert tok == TOKEN, f"게시 경로에 페이지 토큰이 아닌 토큰: {path}"
        if method == "GET" and path == "me":
            r = self._maybe_fail("me")
            if r:
                return r
            body = {"id": PAGE_ID, "name": PAGE_NAME}
            if self.ig_linked:
                body["instagram_business_account"] = {"id": UID, "username": "chulgeun_munjang"}
            return 200, body
        if method == "GET" and path.endswith("/content_publishing_limit"):
            return 200, {"data": [{"quota_usage": 1, "config": {"quota_total": 50, "quota_duration": 86400}}]}
        if method == "GET" and path == f"{UID}/media":
            r = self._maybe_fail("recent")
            if r:
                return r
            return 200, {"data": list(self.recent)}
        if method == "POST" and path == f"{UID}/media":
            if params.get("media_type") == "CAROUSEL":
                self.last_caption = params.get("caption")
                return 200, {"id": self._id("car")}
            r = self._maybe_fail("child")
            if r:
                return r
            if self.reject_alt_text and "alt_text" in params:
                return 400, {"error": {"message": "Invalid parameter alt_text", "code": 100}}
            return 200, {"id": self._id("child")}
        if method == "POST" and path == f"{UID}/media_publish":
            if self.publish_side_effect_before_error:
                mid = self._id("media")
                self.published.append(mid)
                self.recent.insert(0, {"id": mid, "caption": self.last_caption,
                                       "permalink": f"https://www.instagram.com/p/{mid}/",
                                       "timestamp": "2026-11-08T22:18:00+0000"})
            r = self._maybe_fail("publish")
            if r:
                return r
            mid = self._id("media")
            self.published.append(mid)
            self.recent.insert(0, {"id": mid, "caption": self.last_caption,
                                   "permalink": f"https://www.instagram.com/p/{mid}/",
                                   "timestamp": "2026-11-08T22:18:00+0000"})
            return 200, {"id": mid}
        if method == "GET" and params.get("fields", "").startswith("status_code"):
            seq = self.status_seq.get(path)
            code = seq.pop(0) if seq else self.default_status
            return 200, {"status_code": code, "status": "Error: test" if code == "ERROR" else ""}
        if method == "GET" and path.startswith("media"):
            return 200, {"id": path, "permalink": f"https://www.instagram.com/p/{path}/",
                         "timestamp": "2026-11-08T22:18:00+0000"}
        return 404, {"error": {"message": f"unknown route {method} {path}", "code": 803}}

    def count(self, method, path_suffix):
        return sum(1 for m, p, _ in self.calls if m == method and p.endswith(path_suffix))
