"""맥 예약 트리거의 '할 일 있을 때만 dispatch' 판정(remote_due.py) 테스트. 네트워크 없음."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Dict, List

import helpers  # noqa: F401  (scripts/ 를 sys.path 에 넣음)
import queue_lib as ql
import remote_due as rd


def post(slug: str, at: str, approved: bool = True, **extra) -> dict:
    d = {"schema": 1, "slug": slug, "scheduled_at": at, "approved": approved,
         "caption": "캡션", "images": [{"file": "01.jpg"}, {"file": "02.jpg"}]}
    d.update(extra)
    return d


class FakeGh:
    """gh api 를 흉내 낸다. posts: slug→post.json, posted: posted.json 이 있는 slug."""

    def __init__(self, posts: Dict[str, dict], posted=(), truncated=False, fail_on: str = ""):
        self.posts, self.posted, self.truncated, self.fail_on = posts, set(posted), truncated, fail_on
        self.calls: List[List[str]] = []

    def __call__(self, args: List[str]) -> str:
        self.calls.append(args)
        url = args[-1]
        if self.fail_on and self.fail_on in url:
            raise rd.RemoteError("HTTP 502")
        if "/git/trees/" in url:
            tree = [{"path": "README.md", "type": "blob"}, {"path": "queue", "type": "tree"}]
            for slug in self.posts:
                tree.append({"path": f"queue/{slug}", "type": "tree"})
                tree.append({"path": f"queue/{slug}/post.json", "type": "blob"})
                tree.append({"path": f"queue/{slug}/01.jpg", "type": "blob"})
                if slug in self.posted:
                    tree.append({"path": f"queue/{slug}/posted.json", "type": "blob"})
            return json.dumps({"tree": tree, "truncated": self.truncated})
        if "/contents/queue/" in url:
            slug = url.split("/contents/queue/")[1].split("/")[0]
            return json.dumps(self.posts[slug])
        raise AssertionError(url)


def run_main(gh: FakeGh, now: str):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rd.main(["--now", now], run=gh)
    return code, out.getvalue().strip(), err.getvalue()


S = "2026-11-23-x-1"
AT = "2026-11-23T07:17+09:00"


class JudgeTest(unittest.TestCase):
    def test_due_when_approved_unposted_and_time_passed(self):
        code, out, _ = run_main(FakeGh({S: post(S, AT)}), "2026-11-23 07:17")
        self.assertEqual((code, out), (rd.EXIT_DUE, S))

    def test_due_from_10_minutes_before(self):
        # publish.py 의 DUE_GRACE 와 같은 기준이어야 07:17 트리거가 헛돌지 않는다
        self.assertEqual(run_main(FakeGh({S: post(S, AT)}), "2026-11-23 07:07")[0], rd.EXIT_DUE)
        self.assertEqual(run_main(FakeGh({S: post(S, AT)}), "2026-11-23 07:06")[0], rd.EXIT_NOTHING)

    def test_nothing_when_posted(self):
        gh = FakeGh({S: post(S, AT)}, posted=[S])
        code, out, _ = run_main(gh, "2026-11-23 07:47")
        self.assertEqual((code, out), (rd.EXIT_NOTHING, ""))
        # 게시된 항목은 post.json 을 내려받지도 않는다(호출 1번 = 트리)
        self.assertEqual(len(gh.calls), 1)

    def test_nothing_when_unapproved(self):
        code, _, err = run_main(FakeGh({S: post(S, AT, approved=False)}), "2026-11-23 07:17")
        self.assertEqual(code, rd.EXIT_NOTHING)
        self.assertIn("승인 전", err)

    def test_nothing_when_stale_after_20h(self):
        # 재부팅 후 로그인이 하루 넘게 늦으면 publish.py 도 올리지 않으므로 dispatch 하지 않는다
        self.assertEqual(run_main(FakeGh({S: post(S, AT)}), "2026-11-24 03:17")[0], rd.EXIT_DUE)
        code, _, err = run_main(FakeGh({S: post(S, AT)}), "2026-11-24 03:18")
        self.assertEqual(code, rd.EXIT_NOTHING)
        self.assertIn("수동 게시", err)

    def test_nothing_when_skip(self):
        self.assertEqual(run_main(FakeGh({S: post(S, AT, skip=True)}), "2026-11-23 08:00")[0],
                         rd.EXIT_NOTHING)

    def test_empty_queue_and_future_only(self):
        self.assertEqual(run_main(FakeGh({}), "2026-11-23 07:17")[0], rd.EXIT_NOTHING)
        code, _, err = run_main(FakeGh({S: post(S, AT)}), "2026-11-22 22:00")
        self.assertEqual(code, rd.EXIT_NOTHING)
        self.assertIn("다음 예약: " + S, err)

    def test_reboot_catch_up_picks_missed_item_only(self):
        # 월요일 07:17 을 정전으로 놓치고 10:00 에 로그인: 월요일 것만 대상, 수요일 것은 아님
        a, b, c = "2026-11-20-x-0", S, "2026-11-25-x-2"
        gh = FakeGh({a: post(a, "2026-11-20T07:17+09:00"), b: post(b, AT),
                     c: post(c, "2026-11-25T07:17+09:00")}, posted=[a])
        self.assertEqual(run_main(gh, "2026-11-23 10:00")[:2], (rd.EXIT_DUE, b))

    def test_naive_time_is_kst(self):
        self.assertEqual(run_main(FakeGh({S: post(S, "2026-11-23T07:17")}), "2026-11-23 07:17")[0],
                         rd.EXIT_DUE)

    def test_errors_are_reported_not_silent(self):
        for gh in (FakeGh({S: post(S, AT)}, truncated=True),
                   FakeGh({S: post(S, AT)}, fail_on="/git/trees/"),
                   FakeGh({S: post(S, AT)}, fail_on="/contents/"),
                   FakeGh({S: {"slug": S, "approved": True}})):           # scheduled_at 없음
            code, out, err = run_main(gh, "2026-11-23 07:17")
            self.assertEqual(code, rd.EXIT_ERROR)
            self.assertEqual(out, "")
            self.assertIn("판정 실패", err)

    def test_unposted_slugs_ignores_other_paths(self):
        paths = ["queue/a/post.json", "queue/a/posted.json", "queue/b/post.json",
                 "queue/b/01.jpg", "archive/queue/c/post.json", "queue/post.json"]
        self.assertEqual(rd.unposted_slugs(paths), ["b"])

    def test_matches_publish_selection(self):
        # 같은 큐에 대해 publish.py 가 쓰는 ql.select 와 판정이 같아야 한다
        posts = {"p1": post("p1", "2026-11-23T07:17+09:00"),
                 "p2": post("p2", "2026-11-23T07:20+09:00", approved=False),
                 "p3": post("p3", "2026-11-21T07:17+09:00"),
                 "p4": post("p4", "2026-11-23T09:00+09:00")}
        now = ql.parse_time("2026-11-23T07:30")
        items = [ql.QueueItem(s, None, d) for s, d in posts.items()]
        expected = [it.slug for it in ql.select(sorted(items, key=lambda i: i.scheduled_at), now).due]
        due, _ = rd.judge([f"queue/{s}/post.json" for s in posts], posts.__getitem__, now)
        self.assertEqual(due, expected)
        self.assertEqual(due, ["p1"])


if __name__ == "__main__":
    unittest.main()
