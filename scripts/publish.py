#!/usr/bin/env python3
"""출근문장 캐러셀 게시기.

사용법
  python3 scripts/publish.py run [--dry-run] [--slug SLUG [--ignore-schedule]] [--now ISO]
  python3 scripts/publish.py check            # 토큰·페이지·IG 연결·권한·게시 한도 확인(게시 안 함)

환경변수
  FB_PAGE_ACCESS_TOKEN (필수, dry-run 제외) Facebook 페이지 액세스 토큰(만료 없음, setup_token.py 가 저장)
  IG_USER_ID           (선택) 비우면 페이지의 instagram_business_account 로 조회
  GRAPH_API_VERSION    (선택) 기본 v27.0
  PAGES_BASE_URL       (run 필수, dry-run 은 선택) 예: https://<계정>.github.io/<저장소>

종료 코드: 0 정상(할 일 없음 포함), 1 실패 또는 사람 확인 필요, 2 설정 오류
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_lib as ql  # noqa: E402
from ig_api import ApiError, InstagramClient, head_check, token_health, DEFAULT_VERSION  # noqa: E402


class Reporter:
    """콘솔 출력 + GitHub Actions 요약(GITHUB_STEP_SUMMARY)."""

    def __init__(self, out: Callable[[str], None] = print):
        self.out = out
        self.lines: List[str] = []

    def __call__(self, msg: str = "") -> None:
        self.out(msg)
        self.lines.append(msg)

    def flush_summary(self) -> None:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write("```\n" + "\n".join(self.lines) + "\n```\n")


def load_dotenv(path: Path) -> None:
    """로컬 실행용 .env 읽기(이미 있는 환경변수는 덮지 않음)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def make_client(log: Callable[[str], None], transport=None,
                sleep=None) -> InstagramClient:
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not token:
        raise ConfigError("FB_PAGE_ACCESS_TOKEN 이 설정되지 않았습니다(GitHub Secret 또는 .env). "
                          "SETUP.md 9단계 setup_token.py 를 실행하세요")
    kwargs = {"version": os.environ.get("GRAPH_API_VERSION", DEFAULT_VERSION).strip() or DEFAULT_VERSION,
              "log": log}
    if transport is not None:
        kwargs["transport"] = transport
    if sleep is not None:
        kwargs["sleep"] = sleep
    return InstagramClient(token, **kwargs)


class ConfigError(Exception):
    pass


def linked_ig_account(client: InstagramClient, log) -> dict:
    """페이지 토큰의 /me(=페이지)에서 연결된 IG 계정을 찾는다."""
    page = client.page_info()
    ig = page.get("instagram_business_account") or {}
    if not ig.get("id"):
        raise ApiError(f"페이지 '{page.get('name', '?')}' 에 연결된 인스타 프로페셔널 계정이 없거나, "
                       f"저장된 토큰이 페이지 토큰이 아닙니다.\n"
                       f"  → 인스타 앱에서 페이지 연결(SETUP.md 3단계)을 확인한 뒤 setup_token.py 를 다시 실행하세요")
    log(f"페이지 확인: {page.get('name', '?')} → 인스타 @{ig.get('username', '?')} (IG 사용자 ID {ig['id']})")
    return ig


def resolve_user_id(client: InstagramClient, log) -> str:
    uid = os.environ.get("IG_USER_ID", "").strip()
    if uid:
        return uid
    return str(linked_ig_account(client, log)["id"])


def find_already_posted(client: InstagramClient, ig_user_id: str,
                        item: ql.QueueItem) -> Optional[dict]:
    """posted.json 기록이 빠졌더라도 실제 계정에 같은 캡션 글이 있으면 찾아낸다."""
    for m in client.recent_media(ig_user_id, limit=10):
        if ql.caption_matches(m.get("caption", ""), item.caption):
            return m
    return None


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 게시 1건

def publish_item(item: ql.QueueItem, client: InstagramClient, ig_user_id: str,
                 base_url: str, log, check_url=head_check,
                 poll_interval: float = 15.0, poll_timeout: float = 300.0) -> dict:
    # 1) 로컬 기록상 중복 방지
    if item.is_posted:
        log(f"[건너뜀] {item.slug}: 이미 게시됨 {item.posted.get('permalink')}")
        return item.posted

    # 2) 계정 실제 상태로 중복 방지(기록 커밋이 실패했던 경우 대비)
    found = find_already_posted(client, ig_user_id, item)
    if found:
        log(f"[복구] {item.slug}: 같은 캡션의 게시물이 이미 계정에 있습니다 → 기록만 남기고 게시하지 않음")
        rec = {"media_id": found.get("id"), "permalink": found.get("permalink"),
               "posted_at": found.get("timestamp"), "recovered": True,
               "recorded_at": utc_now_iso()}
        ql.write_posted(item, rec)
        return rec

    # 3) 게시 한도
    try:
        lim = client.publishing_limit(ig_user_id)
        usage = lim.get("quota_usage")
        quota = (lim.get("config") or {}).get("quota_total")
        if usage is not None and quota and usage >= quota:
            raise ApiError(f"24시간 게시 한도 초과({usage}/{quota})")
        log(f"게시 한도: {usage}/{quota} (24시간)")
    except ApiError as e:
        if "한도 초과" in str(e):
            raise
        log(f"  경고: 게시 한도 조회 실패, 계속 진행: {e}")

    # 4) 이미지 공개 URL 확인
    urls = [ql.image_url(base_url, item, img["file"]) for img in item.images]
    for u in urls:
        ok, why = check_url(u)
        if not ok:
            raise ApiError(f"이미지 URL 을 열 수 없습니다({why}): {u}\n"
                           f"  → GitHub Pages 가 켜져 있는지, 푸시 후 배포가 끝났는지 확인하세요")
    log(f"이미지 URL {len(urls)}개 확인")

    # 5) 이미지별 컨테이너 → FINISHED
    children = []
    for img, u in zip(item.images, urls):
        cid = client.create_carousel_item(ig_user_id, u, img.get("alt_text"))
        log(f"  항목 컨테이너 {img['file']} → {cid}")
        children.append(cid)
    for cid in children:
        client.wait_finished(cid, interval=poll_interval, timeout=poll_timeout)
    log("  항목 컨테이너 모두 FINISHED")

    # 6) 캐러셀 컨테이너 → FINISHED
    carousel_id = client.create_carousel(ig_user_id, children, item.caption)
    log(f"  캐러셀 컨테이너 → {carousel_id}")
    client.wait_finished(carousel_id, interval=poll_interval, timeout=poll_timeout)

    # 7) 게시. 실패하면 다시 누르기 전에 실제로 올라갔는지 먼저 확인
    try:
        media_id = client.media_publish(ig_user_id, carousel_id)
    except ApiError as e:
        if e.transient:
            log(f"  게시 응답 오류({e}) → 실제로 올라갔는지 확인합니다")
            found = find_already_posted(client, ig_user_id, item)
            if found:
                media_id = found["id"]
            else:
                raise
        else:
            raise

    # 8) 즉시 기록(퍼머링크 조회가 실패해도 중복 게시는 막히도록)
    rec = {"media_id": media_id, "permalink": None, "posted_at": utc_now_iso(),
           "carousel_container_id": carousel_id, "children": children}
    ql.write_posted(item, rec)
    try:
        info = client.media_info(media_id)
        rec["permalink"] = info.get("permalink")
        if info.get("timestamp"):
            rec["posted_at"] = info["timestamp"]
        ql.write_posted(item, rec)
    except ApiError as e:
        log(f"  경고: permalink 조회 실패(게시는 완료): {e}")
    log(f"[게시 완료] {item.slug} → {rec.get('permalink') or media_id}")
    return rec


# ------------------------------------------------------------------ 명령

def describe_plan(item: ql.QueueItem, base_url: str, log) -> None:
    log(f"  예약 시각: {item.scheduled_at.astimezone(ql.KST):%Y-%m-%d %H:%M} KST")
    log(f"  이미지 {len(item.images)}장:")
    for img in item.images:
        u = ql.image_url(base_url, item, img["file"])
        alt = (img.get("alt_text") or "").replace("\n", " ")
        log(f"    - {u}")
        log(f"      alt: {alt[:70]}{'…' if len(alt) > 70 else ''}")
    first = item.caption.splitlines()[0] if item.caption else ""
    log(f"  캡션 {len(item.caption)}자, 해시태그 {ql.count_hashtags(item.caption)}개, 첫 줄: {first}")
    log("  할 일: 항목 컨테이너 생성 → FINISHED 확인 → CAROUSEL 컨테이너 → media_publish → posted.json 기록")


def cmd_run(args, log, client_factory=make_client, check_url=head_check,
            poll_interval: float = 15.0) -> int:
    queue_dir = Path(args.queue_dir) if args.queue_dir else ql.QUEUE_DIR
    now = ql.parse_time(args.now) if args.now else ql.now_kst()
    base_url = (args.base_url or os.environ.get("PAGES_BASE_URL", "")).strip()
    if not base_url:
        if args.dry_run:
            base_url = "https://<계정>.github.io/<저장소>"
        else:
            log("설정 오류: PAGES_BASE_URL 이 없습니다")
            return 2

    items = ql.load_queue(queue_dir)
    mode = "DRY-RUN(Meta API 호출 없음)" if args.dry_run else "실행"
    log(f"== 출근문장 게시기 [{mode}] 기준 시각 {now.astimezone(ql.KST):%Y-%m-%d %H:%M} KST, 큐 {len(items)}건 ==")

    problems: List[str] = []

    if args.slug:
        targets = [it for it in items if it.slug == args.slug]
        if not targets:
            log(f"큐에 {args.slug} 가 없습니다")
            return 1
        it = targets[0]
        if it.is_posted:
            log(f"[건너뜀] {it.slug}: 이미 게시됨 {it.posted.get('permalink')}")
            return 0
        if not it.approved:
            if not args.dry_run:
                log(f"[보류] {it.slug}: 승인되지 않았습니다(post.json approved=false)")
                return 1
            log(f"알림: {it.slug} 는 승인 전입니다. 실제 실행이면 올리지 않고 멈춥니다(아래는 미리보기)")
        if not args.ignore_schedule and it.scheduled_at - ql.DUE_GRACE > now:
            log(f"[대기] {it.slug}: 예약 시각 전입니다. 지금 올리려면 ignore_schedule 를 켜세요")
            if not args.dry_run:
                return 0
        due = [it]
    else:
        items = [it for it in items if not it.data.get("skip")]
        sel = ql.select(items, now)
        for it in sel.unapproved_due:
            problems.append(f"{it.slug}: 예약 시각이 지났지만 승인되지 않아 올리지 않았습니다")
        for it in sel.stale:
            problems.append(f"{it.slug}: 예약 시각({it.data['scheduled_at']})에서 너무 많이 지나 자동으로 올리지 않았습니다. "
                            f"수동 실행(slug 지정)으로 올리거나 날짜를 고치세요")
        due = sel.due[: args.max]
        if len(sel.due) > args.max:
            log(f"알림: 올릴 항목이 {len(sel.due)}건이지만 한 번에 {args.max}건만 올립니다. 나머지는 다음 실행에서")

    pending = [it for it in items if not it.is_posted and it not in due]
    if not due:
        log("지금 올릴 항목이 없습니다.")

    client = None
    ig_user_id = None
    for it in due:
        log(f"\n▶ {it.slug} — {it.data.get('title', '')}")
        try:
            warnings = ql.validate_item(it, require_images=True)
        except ql.QueueError as e:
            problems.append(str(e))
            log(f"  [오류] {e}")
            continue
        for w in warnings:
            log(f"  경고: {w}")
        if args.dry_run:
            describe_plan(it, base_url, log)
            if args.check_urls:
                for img in it.images:
                    u = ql.image_url(base_url, it, img["file"])
                    ok, why = check_url(u)
                    log(f"    URL 확인 {'OK' if ok else '실패(' + why + ')'}: {img['file']}")
                    if not ok:
                        problems.append(f"{it.slug}: 이미지 URL 확인 실패 {u} ({why})")
            continue
        try:
            if client is None:
                client = client_factory(log)
                ig_user_id = resolve_user_id(client, log)
            publish_item(it, client, ig_user_id, base_url, log, check_url=check_url,
                         poll_interval=poll_interval)
        except ConfigError as e:
            log(f"설정 오류: {e}")
            return 2
        except (ApiError, ql.QueueError) as e:
            problems.append(f"{it.slug}: {e}")
            log(f"  [실패] {e}")

    if pending:
        log("\n다음 예정:")
        for it in pending[:6]:
            state = "승인됨" if it.approved else "승인 대기"
            img = "이미지 준비" if ql.images_ready(it) else "이미지 없음"
            log(f"  - {it.scheduled_at.astimezone(ql.KST):%m-%d(%a) %H:%M} {it.slug} [{state}, {img}]")

    if problems:
        log("\n확인 필요:")
        for p in problems:
            log(f"  - {p}")
        return 1
    return 0


def cmd_check(args, log, client_factory=make_client,
              now_ts: Optional[int] = None) -> int:
    """게시하지 않고 확인만: 페이지 토큰, IG 연결, 권한, 토큰 만료, 게시 한도.

    매주 '토큰 점검' 워크플로가 이 명령을 돌린다. 문제가 있으면 1 을 돌려 이슈를 만든다.
    """
    problems: List[str] = []
    try:
        client = client_factory(log)
        ig = linked_ig_account(client, log)
        env_uid = os.environ.get("IG_USER_ID", "").strip()
        if env_uid and env_uid != str(ig["id"]):
            problems.append(f"IG_USER_ID Secret({env_uid})이 페이지에 연결된 계정({ig['id']})과 다릅니다. "
                            f"setup_token.py 를 다시 실행하세요")
        lim = client.publishing_limit(str(ig["id"]))
        log(f"게시 한도: {lim.get('quota_usage')}/{(lim.get('config') or {}).get('quota_total')} (24시간)")
    except ConfigError as e:
        log(f"설정 오류: {e}")
        return 2
    except ApiError as e:
        log(f"[실패] {e}")
        if e.code == 190:
            log("  → 토큰이 무효입니다. SETUP.md 9단계(setup_token.py)를 다시 하세요")
        return 1

    # 토큰 상세는 보조 확인: 조회가 안 돼도 위 확인이 통과했으면 실패로 보지 않는다
    try:
        info = client.debug_token()
    except ApiError as e:
        log(f"  참고: 토큰 상세(debug_token) 조회 실패, 건너뜀: {e}")
    else:
        bad, notes = token_health(info, int(now_ts if now_ts is not None else time.time()))
        for n in notes:
            log(f"  {n}")
        problems.extend(bad)

    if problems:
        log("\n확인 필요:")
        for p in problems:
            log(f"  - {p}")
        return 1
    log("토큰·계정 확인 완료(게시하지 않음)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="출근문장 인스타그램 캐러셀 게시기(Facebook 로그인 방식)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="예약 시각이 지난 승인 항목 게시")
    r.add_argument("--dry-run", action="store_true", help="Meta API 호출 없이 계획만 출력")
    r.add_argument("--slug", help="이 항목만 대상으로")
    r.add_argument("--ignore-schedule", action="store_true", help="--slug 와 함께: 예약 시각 전이어도 게시")
    r.add_argument("--max", type=int, default=1, help="한 번에 올릴 최대 건수(기본 1)")
    r.add_argument("--now", help="기준 시각(ISO, 시간대 없으면 KST). 시험용")
    r.add_argument("--base-url", help="이미지 공개 주소 기본값(PAGES_BASE_URL 대신)")
    r.add_argument("--queue-dir", help="큐 폴더(기본 queue/)")
    r.add_argument("--check-urls", action="store_true", help="dry-run 에서 이미지 URL 열림 확인")

    sub.add_parser("check", help="토큰·페이지·IG 연결·권한·게시 한도 확인(게시 안 함)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv(ql.ROOT / ".env")
    args = build_parser().parse_args(argv)
    log = Reporter()
    try:
        if args.cmd == "run":
            if args.ignore_schedule and not args.slug:
                log("설정 오류: --ignore-schedule 는 --slug 와 함께만 쓸 수 있습니다")
                return 2
            return cmd_run(args, log)
        if args.cmd == "check":
            return cmd_check(args, log)
        return 2
    finally:
        log.flush_summary()


if __name__ == "__main__":
    sys.exit(main())
