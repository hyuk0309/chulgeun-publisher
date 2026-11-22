#!/usr/bin/env python3
"""원격 main 기준으로 '지금 올릴 항목이 있는가'만 판정한다(게시하지 않음).

맥의 예약 트리거(scripts/mac_trigger.sh)가 dispatch 전에 부른다. 로컬 작업트리·로컬 git 은
보지 않고 GitHub API(gh api)로 원격 main 의 queue/ 상태를 읽는다.

판정은 publish.py run(slug 없음)과 같은 queue_lib.select 를 쓴다:
  승인됨 + posted.json 없음 + skip 아님 + 예약 10분 전 이후 + 예약 후 20시간 이내

사용법
  python3 scripts/remote_due.py [--repo OWNER/NAME] [--ref main] [--now ISO]

종료 코드: 0 올릴 항목 있음(표준출력에 slug), 3 할 일 없음, 2 판정 실패(네트워크·API·형식 오류)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_lib as ql  # noqa: E402

DEFAULT_REPO = "hyuk0309/chulgeun-publisher"
EXIT_DUE, EXIT_NOTHING, EXIT_ERROR = 0, 3, 2

Runner = Callable[[List[str]], str]


class RemoteError(Exception):
    pass


def gh_runner(args: List[str]) -> str:
    """gh api 호출. 실패하면 RemoteError."""
    try:
        p = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RemoteError(f"gh 실행 실패: {e}")
    if p.returncode != 0:
        raise RemoteError(f"gh api {' '.join(args)} 실패(종료 {p.returncode}): {p.stderr.strip()[:300]}")
    return p.stdout


def list_paths(run: Runner, repo: str, ref: str) -> List[str]:
    raw = run([f"repos/{repo}/git/trees/{ref}?recursive=1"])
    try:
        tree = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RemoteError(f"트리 응답 JSON 오류: {e}")
    if tree.get("truncated"):
        raise RemoteError("트리 응답이 잘렸습니다(truncated). 큐 상태를 확정할 수 없습니다")
    return [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]


def unposted_slugs(paths: List[str]) -> List[str]:
    """queue/<slug>/post.json 은 있고 queue/<slug>/posted.json 은 없는 slug."""
    posts, posted = set(), set()
    for p in paths:
        parts = p.split("/")
        if len(parts) != 3 or parts[0] != "queue":
            continue
        if parts[2] == ql.POST_FILE:
            posts.add(parts[1])
        elif parts[2] == ql.POSTED_FILE:
            posted.add(parts[1])
    return sorted(posts - posted)


def fetch_post(run: Runner, repo: str, ref: str, slug: str) -> dict:
    raw = run(["-H", "Accept: application/vnd.github.raw+json",
               f"repos/{repo}/contents/queue/{slug}/{ql.POST_FILE}?ref={ref}"])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RemoteError(f"queue/{slug}/post.json JSON 오류: {e}")


def judge(paths: List[str], get_post: Callable[[str], dict],
          now: datetime) -> Tuple[List[str], List[str]]:
    """(올릴 slug 목록, 참고 메모) 를 돌려준다. 형식 오류는 RemoteError."""
    items = []
    for slug in unposted_slugs(paths):
        data = get_post(slug)
        it = ql.QueueItem(slug=slug, path=Path("queue") / slug, data=data, posted=None)
        if data.get("skip"):
            continue
        try:
            it.scheduled_at
        except (KeyError, ValueError, TypeError):
            raise RemoteError(f"{slug}: scheduled_at 이 없거나 형식이 잘못됐습니다")
        items.append(it)
    items.sort(key=lambda it: (it.scheduled_at, it.slug))
    sel = ql.select(items, now)
    notes = [f"{it.slug}: 예약 시각이 지났지만 승인 전(dispatch 안 함)" for it in sel.unapproved_due]
    notes += [f"{it.slug}: 예약 후 {int(ql.STALE_AFTER.total_seconds() // 3600)}시간이 지나 자동 게시 대상 아님"
              f"(dispatch 안 함, 수동 게시 필요)" for it in sel.stale]
    upcoming = [it for it in items if it not in sel.due and it.scheduled_at - ql.DUE_GRACE > now]
    if upcoming:
        nxt = upcoming[0]
        notes.append(f"다음 예약: {nxt.slug} {nxt.scheduled_at.astimezone(ql.KST):%m-%d %H:%M} KST"
                     f" [{'승인됨' if nxt.approved else '승인 대기'}]")
    return [it.slug for it in sel.due], notes


def main(argv: Optional[List[str]] = None, run: Runner = gh_runner) -> int:
    ap = argparse.ArgumentParser(description="원격 main 큐에서 지금 올릴 항목이 있는지 판정")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--now", help="기준 시각(ISO, 시간대 없으면 KST). 시험용")
    args = ap.parse_args(argv)
    now = ql.parse_time(args.now) if args.now else ql.now_kst()
    try:
        paths = list_paths(run, args.repo, args.ref)
        cache: Dict[str, dict] = {}

        def get_post(slug: str) -> dict:
            if slug not in cache:
                cache[slug] = fetch_post(run, args.repo, args.ref, slug)
            return cache[slug]

        due, notes = judge(paths, get_post, now)
    except RemoteError as e:
        print(f"판정 실패: {e}", file=sys.stderr)
        return EXIT_ERROR
    for n in notes:
        print(f"참고: {n}", file=sys.stderr)
    if due:
        print(" ".join(due))
        return EXIT_DUE
    return EXIT_NOTHING


if __name__ == "__main__":
    sys.exit(main())
