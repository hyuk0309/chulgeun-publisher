"""큐(queue/<slug>/post.json) 읽기·검증·선택 공통 로직.

표준 라이브러리만 사용한다(Python 3.9 이상).
"""
from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

KST = timezone(timedelta(hours=9), name="KST")

ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = ROOT / "queue"

POST_FILE = "post.json"
POSTED_FILE = "posted.json"

# 인스타그램 제한(공식 문서 기준)과 채널 규칙
CAPTION_MAX_CHARS = 2200
HASHTAG_MAX = 30
CHANNEL_HASHTAG_COUNT = 5          # 채널 규칙: 해시태그 정확히 5개(경고만)
CAROUSEL_MIN = 2
CAROUSEL_MAX = 10                  # 문서는 20장이지만 10장 안내도 있어 보수적으로 10장
EXPECTED_SIZE = (1080, 1350)

# 예약 판정
DUE_GRACE = timedelta(minutes=10)      # 예약 시각 10분 전부터는 '도래'로 본다
STALE_AFTER = timedelta(hours=20)      # 이보다 오래 밀린 항목은 예약 실행으로 올리지 않는다


class QueueError(Exception):
    """큐 항목이 잘못되어 게시할 수 없을 때."""


@dataclass
class QueueItem:
    slug: str
    path: Path
    data: dict
    posted: Optional[dict] = None

    @property
    def scheduled_at(self) -> datetime:
        return parse_time(self.data["scheduled_at"])

    @property
    def approved(self) -> bool:
        return bool(self.data.get("approved"))

    @property
    def caption(self) -> str:
        return self.data.get("caption", "")

    @property
    def images(self) -> List[dict]:
        return list(self.data.get("images", []))

    @property
    def is_posted(self) -> bool:
        return self.posted is not None and bool(self.posted.get("media_id"))


@dataclass
class Selection:
    due: List[QueueItem] = field(default_factory=list)          # 지금 올릴 대상(순서대로)
    stale: List[QueueItem] = field(default_factory=list)        # 너무 밀려 자동으로는 안 올리는 항목
    unapproved_due: List[QueueItem] = field(default_factory=list)  # 시각이 지났지만 승인 안 된 항목


def parse_time(value: str) -> datetime:
    """ISO 8601 문자열. 시간대가 없으면 KST로 본다."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def load_item(path: Path) -> QueueItem:
    post_path = path / POST_FILE
    try:
        data = json.loads(post_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QueueError(f"{post_path} 가 없습니다")
    except json.JSONDecodeError as e:
        raise QueueError(f"{post_path} JSON 형식 오류: {e}")
    posted = None
    posted_path = path / POSTED_FILE
    if posted_path.exists():
        posted = json.loads(posted_path.read_text(encoding="utf-8"))
    slug = data.get("slug") or path.name
    if slug != path.name:
        raise QueueError(f"{post_path}: slug({slug})가 폴더 이름({path.name})과 다릅니다")
    return QueueItem(slug=slug, path=path, data=data, posted=posted)


def load_queue(queue_dir: Path = QUEUE_DIR) -> List[QueueItem]:
    items = []
    if not queue_dir.exists():
        return items
    for child in sorted(queue_dir.iterdir()):
        if child.is_dir() and (child / POST_FILE).exists():
            items.append(load_item(child))
    items.sort(key=lambda it: (it.scheduled_at, it.slug))
    return items


def select(items: List[QueueItem], now: datetime,
           stale_after: timedelta = STALE_AFTER) -> Selection:
    """예약 실행에서 무엇을 올릴지 고른다. 게시된 항목은 절대 고르지 않는다."""
    sel = Selection()
    for it in items:
        if it.is_posted:
            continue
        if it.scheduled_at - DUE_GRACE > now:
            continue
        if not it.approved:
            sel.unapproved_due.append(it)
            continue
        if now - it.scheduled_at > stale_after:
            sel.stale.append(it)
            continue
        sel.due.append(it)
    return sel


def write_posted(item: QueueItem, record: dict) -> None:
    record = dict(record)
    record.setdefault("slug", item.slug)
    tmp = item.path / (POSTED_FILE + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, item.path / POSTED_FILE)
    item.posted = record


# ---------------------------------------------------------------- 검증

def count_hashtags(caption: str) -> int:
    return len(re.findall(r"(?:^|\s)#[^\s#]+", caption))


def jpeg_size(path: Path) -> Tuple[int, int]:
    """JPEG 헤더에서 (너비, 높이)를 읽는다. JPEG가 아니면 QueueError."""
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            raise QueueError(f"{path.name}: JPEG 파일이 아닙니다")
        while True:
            b = f.read(1)
            if not b:
                break
            if b != b"\xff":
                continue
            marker = f.read(1)
            while marker == b"\xff":
                marker = f.read(1)
            if not marker:
                break
            m = marker[0]
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                continue
            if m == 0xD9:
                break
            seg_len_raw = f.read(2)
            if len(seg_len_raw) < 2:
                break
            seg_len = struct.unpack(">H", seg_len_raw)[0]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                data = f.read(5)
                height, width = struct.unpack(">xHH", data)
                return width, height
            f.seek(seg_len - 2, 1)
    raise QueueError(f"{path.name}: JPEG 크기를 읽지 못했습니다")


def validate_item(item: QueueItem, require_images: bool = True) -> List[str]:
    """오류가 있으면 QueueError, 경고는 문자열 목록으로 돌려준다."""
    warnings: List[str] = []
    d = item.data
    for key in ("scheduled_at", "caption", "images"):
        if key not in d:
            raise QueueError(f"{item.slug}: post.json에 '{key}' 가 없습니다")
    try:
        item.scheduled_at
    except ValueError:
        raise QueueError(f"{item.slug}: scheduled_at 형식이 잘못됐습니다: {d['scheduled_at']!r}")

    cap = item.caption
    if not cap.strip():
        raise QueueError(f"{item.slug}: 캡션이 비어 있습니다")
    if len(cap) > CAPTION_MAX_CHARS:
        raise QueueError(f"{item.slug}: 캡션이 {len(cap)}자로 {CAPTION_MAX_CHARS}자 제한을 넘습니다")
    tags = count_hashtags(cap)
    if tags > HASHTAG_MAX:
        raise QueueError(f"{item.slug}: 해시태그 {tags}개, 최대 {HASHTAG_MAX}개")
    if tags != CHANNEL_HASHTAG_COUNT:
        warnings.append(f"해시태그가 {tags}개입니다(채널 규칙 {CHANNEL_HASHTAG_COUNT}개)")

    imgs = item.images
    if not (CAROUSEL_MIN <= len(imgs) <= CAROUSEL_MAX):
        raise QueueError(f"{item.slug}: 이미지 {len(imgs)}장, 캐러셀은 {CAROUSEL_MIN}~{CAROUSEL_MAX}장")
    names = set()
    for img in imgs:
        name = img.get("file", "")
        if not re.fullmatch(r"[0-9A-Za-z_-]+\.jpe?g", name):
            raise QueueError(f"{item.slug}: 이미지 파일 이름은 영문·숫자 .jpg 여야 합니다: {name!r}")
        if name in names:
            raise QueueError(f"{item.slug}: 이미지 이름 중복: {name}")
        names.add(name)
        if not img.get("alt_text"):
            warnings.append(f"{name}: alt_text 가 비어 있습니다")
        if require_images:
            p = item.path / name
            if not p.exists():
                raise QueueError(f"{item.slug}: 이미지 파일이 아직 없습니다: {name}")
            w, h = jpeg_size(p)
            if (w, h) != EXPECTED_SIZE:
                raise QueueError(f"{item.slug}: {name} 크기 {w}x{h}, 기대값 {EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}")
    return warnings


def images_ready(item: QueueItem) -> bool:
    return all((item.path / img.get("file", "")).exists() for img in item.images)


def normalize_caption(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def caption_matches(a: str, b: str, prefix: int = 80) -> bool:
    """이미 게시된 캡션과 같은 글인지(공백 차이 무시, 앞부분 비교)."""
    na, nb = normalize_caption(a), normalize_caption(b)
    if not na or not nb:
        return False
    return na[:prefix] == nb[:prefix]


def image_url(base_url: str, item: QueueItem, file_name: str) -> str:
    return f"{base_url.rstrip('/')}/queue/{item.slug}/{file_name}"
