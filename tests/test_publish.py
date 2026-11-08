"""게시기 단위 테스트(실제 API 호출 없음). 실행: python3 -m unittest discover -s tests"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from helpers import CAPTION, PAGE_NAME, TOKEN, UID, FakeGraph, fake_jpeg, make_item

import queue_lib as ql
import publish as pub
from ig_api import ApiError, InstagramClient, token_health

BASE = "https://ops.github.io/chulgeun-publisher"
ALWAYS_OK = lambda url: (True, "ok")  # noqa: E731


def run_args(**kw):
    d = dict(dry_run=False, slug=None, ignore_schedule=False, max=1, now=None,
             base_url=BASE, queue_dir=None, check_urls=False)
    d.update(kw)
    return Namespace(**d)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.q = Path(self.tmp.name) / "queue"
        self.q.mkdir()
        self.fake = FakeGraph()
        self.logs = []
        self.env = mock.patch.dict(os.environ, {"FB_PAGE_ACCESS_TOKEN": TOKEN}, clear=False)
        self.env.start()
        os.environ.pop("IG_USER_ID", None)
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        os.environ.pop("GITHUB_ACTIONS", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def log(self, msg=""):
        self.logs.append(msg)

    def client_factory(self, log):
        return InstagramClient(TOKEN, transport=self.fake, sleep=lambda s: None, log=log)

    def run_cmd(self, **kw):
        args = run_args(queue_dir=str(self.q), **kw)
        return pub.cmd_run(args, self.log, client_factory=self.client_factory,
                           check_url=ALWAYS_OK, poll_interval=0)

    def all_logs(self):
        return "\n".join(self.logs)


# ---------------------------------------------------------------- 선택(예약) 로직

class SelectTest(Base):
    def test_selection_rules(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")                      # 도래
        make_item(self.q, "2026-11-11-b", "2026-11-11T07:17+09:00")                      # 미래
        make_item(self.q, "2026-11-08-c", "2026-11-08T07:17+09:00", approved=False)      # 미승인
        make_item(self.q, "2026-11-01-d", "2026-11-01T07:17+09:00")                      # 너무 밀림
        make_item(self.q, "2026-11-07-e", "2026-11-07T07:17+09:00",
                  posted={"media_id": "m1", "permalink": "p"})                           # 게시됨
        items = ql.load_queue(self.q)
        sel = ql.select(items, ql.parse_time("2026-11-09T07:20+09:00"))
        self.assertEqual([i.slug for i in sel.due], ["2026-11-09-a"])
        self.assertEqual([i.slug for i in sel.unapproved_due], ["2026-11-08-c"])
        self.assertEqual([i.slug for i in sel.stale], ["2026-11-01-d"])

    def test_grace_window(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        items = ql.load_queue(self.q)
        self.assertEqual(len(ql.select(items, ql.parse_time("2026-11-09T07:08+09:00")).due), 1)
        self.assertEqual(len(ql.select(items, ql.parse_time("2026-11-09T07:00+09:00")).due), 0)

    def test_naive_time_is_kst(self):
        self.assertEqual(ql.parse_time("2026-11-09T07:17").utcoffset(), timedelta(hours=9))

    def test_slug_must_match_folder(self):
        d = make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        data = json.loads((d / "post.json").read_text())
        data["slug"] = "other"
        (d / "post.json").write_text(json.dumps(data))
        with self.assertRaises(ql.QueueError):
            ql.load_queue(self.q)


# ---------------------------------------------------------------- 검증

class ValidateTest(Base):
    def test_jpeg_size(self):
        p = Path(self.tmp.name) / "x.jpg"
        p.write_bytes(fake_jpeg(1080, 1350))
        self.assertEqual(ql.jpeg_size(p), (1080, 1350))
        p.write_bytes(b"\x89PNG....")
        with self.assertRaises(ql.QueueError):
            ql.jpeg_size(p)

    def test_wrong_size_rejected(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", size=(1080, 1080))
        with self.assertRaises(ql.QueueError):
            ql.validate_item(ql.load_queue(self.q)[0])

    def test_missing_images_rejected(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", write_images=False)
        it = ql.load_queue(self.q)[0]
        with self.assertRaises(ql.QueueError):
            ql.validate_item(it, require_images=True)
        ql.validate_item(it, require_images=False)  # 경로만 걸어 두는 건 허용

    def test_caption_limits(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", caption="가" * 2201)
        with self.assertRaises(ql.QueueError):
            ql.validate_item(ql.load_queue(self.q)[0])

    def test_hashtag_count_warning(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", caption="본문 #하나 #둘")
        warns = ql.validate_item(ql.load_queue(self.q)[0])
        self.assertTrue(any("해시태그" in w for w in warns))

    def test_carousel_count(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", n_images=1)
        with self.assertRaises(ql.QueueError):
            ql.validate_item(ql.load_queue(self.q)[0])

    def test_caption_match_ignores_whitespace(self):
        self.assertTrue(ql.caption_matches(CAPTION, CAPTION.replace("\n", "  \n ")))
        self.assertFalse(ql.caption_matches(CAPTION, "다른 글 " + CAPTION))
        self.assertFalse(ql.caption_matches("", CAPTION))


# ---------------------------------------------------------------- 게시 흐름

class PublishFlowTest(Base):
    def test_happy_path(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", n_images=3)
        rc = self.run_cmd(now="2026-11-09T07:18+09:00")
        self.assertEqual(rc, 0, self.all_logs())
        # 호출 순서: me(페이지→IG 계정) → 최근 게시물(중복 확인) → 한도 → 항목 3 → 상태 3 → 캐러셀 → 상태 → 게시 → permalink
        seq = [(m, p) for m, p, _ in self.fake.calls]
        self.assertEqual(seq[0], ("GET", "me"))
        self.assertEqual(seq[1], ("GET", f"{UID}/media"))
        self.assertEqual(self.fake.count("POST", f"{UID}/media"), 4)
        self.assertEqual(self.fake.count("POST", "media_publish"), 1)
        child_posts = [p for m, path, p in self.fake.calls
                       if m == "POST" and path == f"{UID}/media" and p.get("is_carousel_item")]
        self.assertEqual(child_posts[0]["image_url"], f"{BASE}/queue/2026-11-09-a/01.jpg")
        self.assertEqual(child_posts[0]["alt_text"], "1/3 카드")
        carousel = [p for m, path, p in self.fake.calls if p.get("media_type") == "CAROUSEL"][0]
        self.assertEqual(carousel["children"], "child1,child2,child3")
        self.assertEqual(carousel["caption"], CAPTION)
        # 게시는 모든 컨테이너가 FINISHED 확인된 뒤에만
        idx_publish = seq.index(("POST", f"{UID}/media_publish"))
        idx_last_status = max(i for i, (m, p) in enumerate(seq) if p in ("child1", "child2", "child3", "car4"))
        self.assertLess(idx_last_status, idx_publish)
        posted = json.loads((self.q / "2026-11-09-a" / "posted.json").read_text())
        self.assertEqual(posted["media_id"], "media5")
        self.assertEqual(posted["permalink"], "https://www.instagram.com/p/media5/")
        self.assertNotIn(TOKEN, self.all_logs())
        self.assertIn(PAGE_NAME, self.all_logs())

    def test_ig_user_id_env_skips_lookup(self):
        os.environ["IG_USER_ID"] = UID
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 0, self.all_logs())
        self.assertEqual(self.fake.count("GET", "me"), 0)
        self.assertEqual(len(self.fake.published), 1)

    def test_page_without_ig_link_publishes_nothing(self):
        self.fake.ig_linked = False
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 1)
        self.assertEqual(self.fake.count("POST", ""), 0)
        self.assertIn("SETUP.md 3단계", self.all_logs())
        self.assertFalse((self.q / "2026-11-09-a" / "posted.json").exists())

    def test_rerun_does_not_repost(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 0)
        self.fake.calls.clear()
        self.assertEqual(self.run_cmd(now="2026-11-09T09:18+09:00"), 0)   # 백업 실행
        self.assertEqual(self.fake.calls, [])  # API 를 아예 부르지 않음
        self.assertEqual(len(self.fake.published), 1)

    def test_slug_rerun_of_posted_item_is_noop(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00",
                  posted={"media_id": "m1", "permalink": "p"})
        rc = self.run_cmd(slug="2026-11-09-a", ignore_schedule=True, now="2026-11-09T07:18+09:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.calls, [])

    def test_recovers_when_record_was_lost(self):
        """게시는 됐는데 posted.json 커밋이 실패한 경우: 계정에서 찾아 기록만 남긴다."""
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        self.fake.recent = [{"id": "m9", "caption": CAPTION, "permalink": "https://www.instagram.com/p/m9/",
                             "timestamp": "2026-11-08T22:17:30+0000"}]
        self.assertEqual(self.run_cmd(now="2026-11-09T09:18+09:00"), 0)
        self.assertEqual(self.fake.count("POST", f"{UID}/media"), 0)
        self.assertEqual(self.fake.count("POST", "media_publish"), 0)
        posted = json.loads((self.q / "2026-11-09-a" / "posted.json").read_text())
        self.assertTrue(posted["recovered"])
        self.assertEqual(posted["media_id"], "m9")

    def test_publish_timeout_but_actually_posted(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        self.fake.publish_side_effect_before_error = True
        self.fake.fail["publish"] = (500, {"message": "temporary", "code": 2, "is_transient": True})
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 0, self.all_logs())
        self.assertEqual(self.fake.count("POST", "media_publish"), 1)  # 다시 누르지 않음
        self.assertTrue((self.q / "2026-11-09-a" / "posted.json").exists())

    def test_publish_error_records_nothing(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        self.fake.fail["publish"] = (400, {"message": "Application request limit reached", "code": 9})
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 1)
        self.assertFalse((self.q / "2026-11-09-a" / "posted.json").exists())
        self.assertIn("확인 필요", self.all_logs())

    def test_container_error_stops_before_publish(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        self.fake.status_seq["child2"] = ["ERROR"]
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 1)
        self.assertEqual(self.fake.count("POST", "media_publish"), 0)
        self.assertEqual(sum(1 for _, _, p in self.fake.calls if p.get("media_type") == "CAROUSEL"), 0)

    def test_polls_until_finished(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", n_images=2)
        self.fake.status_seq["child1"] = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]
        self.fake.status_seq["car3"] = ["IN_PROGRESS", "FINISHED"]
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 0, self.all_logs())
        self.assertEqual(self.fake.count("GET", "child1"), 3)
        self.assertEqual(self.fake.count("GET", "car3"), 2)

    def test_poll_timeout(self):
        c = InstagramClient(TOKEN, transport=self.fake, sleep=lambda s: None, log=self.log)
        self.fake.default_status = "IN_PROGRESS"
        with self.assertRaises(ApiError):
            c.wait_finished("child1", interval=60, timeout=300, first_wait=0)
        self.assertLessEqual(self.fake.count("GET", "child1"), 6)

    def test_bad_image_url_stops_before_containers(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        args = run_args(queue_dir=str(self.q), now="2026-11-09T07:18+09:00")
        rc = pub.cmd_run(args, self.log, client_factory=self.client_factory,
                         check_url=lambda u: (False, "HTTP 404"), poll_interval=0)
        self.assertEqual(rc, 1)
        self.assertEqual(self.fake.count("POST", f"{UID}/media"), 0)
        self.assertIn("GitHub Pages", self.all_logs())

    def test_alt_text_rejected_retries_without(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", n_images=2)
        self.fake.reject_alt_text = True
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 0, self.all_logs())
        self.assertEqual(len(self.fake.published), 1)

    def test_only_one_per_run(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T06:17+09:00")
        make_item(self.q, "2026-11-09-b", "2026-11-09T07:17+09:00",
                  caption="두 번째 글\n#직장인 #직장인공감 #슬럼프 #총균쇠 #출근문장")
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 0)
        self.assertEqual(len(self.fake.published), 1)
        self.assertTrue((self.q / "2026-11-09-a" / "posted.json").exists())  # 오래된 것부터
        self.assertFalse((self.q / "2026-11-09-b" / "posted.json").exists())

    def test_unapproved_due_is_not_posted_and_flagged(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", approved=False)
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 1)
        self.assertEqual(self.fake.calls, [])
        self.assertIn("승인되지 않아", self.all_logs())

    def test_skip_flag(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00", approved=False, extra={"skip": True})
        self.assertEqual(self.run_cmd(now="2026-11-09T07:18+09:00"), 0)

    def test_slug_before_schedule_waits_unless_forced(self):
        make_item(self.q, "2026-11-13-a", "2026-11-13T07:17+09:00")
        self.assertEqual(self.run_cmd(slug="2026-11-13-a", now="2026-11-09T07:18+09:00"), 0)
        self.assertEqual(self.fake.published, [])
        self.assertEqual(self.run_cmd(slug="2026-11-13-a", ignore_schedule=True,
                                      now="2026-11-09T07:18+09:00"), 0)
        self.assertEqual(len(self.fake.published), 1)

    def test_missing_token_is_config_error(self):
        os.environ["FB_PAGE_ACCESS_TOKEN"] = ""
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        args = run_args(queue_dir=str(self.q), now="2026-11-09T07:18+09:00")
        self.assertEqual(pub.cmd_run(args, self.log, check_url=ALWAYS_OK), 2)


class DryRunTest(Base):
    def test_dry_run_makes_no_api_calls(self):
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")

        def boom(log):
            raise AssertionError("dry-run 에서 API 클라이언트를 만들면 안 됨")
        args = run_args(queue_dir=str(self.q), dry_run=True, now="2026-11-09T07:18+09:00")
        rc = pub.cmd_run(args, self.log, client_factory=boom, check_url=ALWAYS_OK)
        self.assertEqual(rc, 0)
        out = self.all_logs()
        self.assertIn("DRY-RUN", out)
        self.assertIn(f"{BASE}/queue/2026-11-09-a/01.jpg", out)
        self.assertIn("media_publish", out)
        self.assertFalse((self.q / "2026-11-09-a" / "posted.json").exists())

    def test_dry_run_previews_unapproved_slug(self):
        make_item(self.q, "2026-11-13-a", "2026-11-13T07:17+09:00", approved=False)
        args = run_args(queue_dir=str(self.q), dry_run=True, slug="2026-11-13-a",
                        now="2026-11-09T07:18+09:00")
        self.assertEqual(pub.cmd_run(args, self.log, client_factory=None, check_url=ALWAYS_OK), 0)
        self.assertIn("승인 전", self.all_logs())
        self.assertIn("media_publish", self.all_logs())

    def test_dry_run_without_token(self):
        os.environ["FB_PAGE_ACCESS_TOKEN"] = ""
        make_item(self.q, "2026-11-09-a", "2026-11-09T07:17+09:00")
        args = run_args(queue_dir=str(self.q), dry_run=True, base_url=None, now="2026-11-09T07:18+09:00")
        self.assertEqual(pub.cmd_run(args, self.log), 0)


# ---------------------------------------------------------------- HTTP 재시도·토큰

class ClientTest(Base):
    def test_get_retried_on_5xx_post_not(self):
        c = InstagramClient(TOKEN, transport=self.fake, sleep=lambda s: None, log=self.log)
        self.fake.fail["recent"] = (500, {"message": "oops", "code": 1})
        self.fake.fail_times["recent"] = 1
        self.assertEqual(c.recent_media(UID), [])
        self.assertEqual(self.fake.count("GET", f"{UID}/media"), 2)

        self.fake.fail["child"] = (500, {"message": "oops", "code": 1})
        with self.assertRaises(ApiError):
            c.create_carousel_item(UID, "https://x/01.jpg")
        self.assertEqual(self.fake.count("POST", f"{UID}/media"), 1)

    def test_token_redacted_in_errors(self):
        def leaky(method, url, params):
            return 400, {"error": {"message": f"bad token {params['access_token']}", "code": 190}}
        c = InstagramClient(TOKEN, transport=leaky, sleep=lambda s: None)
        with self.assertRaises(ApiError) as cm:
            c.page_info()
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_secrets_redacted_everywhere(self):
        c = InstagramClient(TOKEN, transport=lambda m, u, p: (400, {"error": {
            "message": f"bad {p['access_token']} and appSECRET", "code": 190}}),
            sleep=lambda s: None, secrets=["appSECRET"])
        with self.assertRaises(ApiError) as cm:
            c.page_info()
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn("appSECRET", str(cm.exception))


NOW = 1794000000   # 2026-11-07 무렵(UTC 초)
DAY = 86400


class CheckTest(Base):
    def check(self):
        return pub.cmd_check(Namespace(), self.log, client_factory=self.client_factory, now_ts=NOW)

    def test_check_ok_never_expiring_page_token(self):
        self.assertEqual(self.check(), 0, self.all_logs())
        self.assertEqual(self.fake.count("POST", ""), 0)    # 게시하지 않음
        out = self.all_logs()
        self.assertIn("@chulgeun_munjang", out)
        self.assertIn("토큰 만료: 없음", out)
        self.assertNotIn(TOKEN, out)

    def test_check_fails_when_ig_not_linked(self):
        self.fake.ig_linked = False
        self.assertEqual(self.check(), 1)
        self.assertIn("연결된 인스타", self.all_logs())

    def test_check_fails_on_invalid_token(self):
        self.fake.fail["me"] = (400, {"message": "Error validating access token", "code": 190})
        self.assertEqual(self.check(), 1)
        self.assertIn("setup_token.py", self.all_logs())

    def test_check_flags_user_id_mismatch(self):
        os.environ["IG_USER_ID"] = "999"
        self.assertEqual(self.check(), 1)
        self.assertIn("IG_USER_ID", self.all_logs())

    def test_check_flags_data_access_expiring_soon(self):
        self.fake.debug_info[TOKEN]["data_access_expires_at"] = NOW + 5 * DAY
        self.assertEqual(self.check(), 1)
        self.assertIn("데이터 접근 권한 만료까지 약 5일", self.all_logs())

    def test_check_debug_token_unavailable_is_not_fatal(self):
        self.fake.fail["debug"] = (400, {"message": "(#100) access token not allowed", "code": 100})
        self.assertEqual(self.check(), 0, self.all_logs())
        self.assertIn("debug_token", self.all_logs())

    def test_missing_token_is_config_error(self):
        os.environ["FB_PAGE_ACCESS_TOKEN"] = ""
        self.assertEqual(pub.cmd_check(Namespace(), self.log), 2)


class TokenHealthTest(unittest.TestCase):
    OK = {"type": "PAGE", "is_valid": True, "expires_at": 0, "data_access_expires_at": 0,
          "scopes": ["instagram_basic", "instagram_content_publish", "pages_read_engagement"]}

    def health(self, **kw):
        info = dict(self.OK)
        info.update(kw)
        return token_health(info, NOW)

    def test_ok(self):
        bad, notes = self.health()
        self.assertEqual(bad, [])
        self.assertIn("토큰 만료: 없음", notes)

    def test_user_token_rejected(self):
        bad, _ = self.health(type="USER", expires_at=NOW + 60 * DAY)
        self.assertTrue(any("USER" in b for b in bad))

    def test_expiring_soon(self):
        bad, _ = self.health(expires_at=NOW + 3 * DAY)
        self.assertTrue(any("만료까지 약 3일" in b for b in bad))
        bad, notes = self.health(expires_at=NOW + 50 * DAY)
        self.assertEqual(bad, [])

    def test_missing_publish_scope(self):
        bad, _ = self.health(scopes=["instagram_basic", "pages_read_engagement"])
        self.assertTrue(any("instagram_content_publish" in b for b in bad))

    def test_invalid(self):
        bad, _ = self.health(is_valid=False)
        self.assertEqual(len(bad), 1)


if __name__ == "__main__":
    unittest.main()
