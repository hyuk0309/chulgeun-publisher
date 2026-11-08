"""setup_token.py 테스트(실제 Meta·GitHub 호출 없음). gh 는 가짜 실행기로 대신한다."""
from __future__ import annotations

import unittest
from argparse import Namespace

from helpers import (APP_ID, APP_SECRET, LONG_USER_TOKEN, PAGE_ID, SHORT_TOKEN, TOKEN, UID,
                     FakeGraph)

import setup_token as st

NOW = 1794000000
ALL_SECRETS = (APP_SECRET, SHORT_TOKEN, LONG_USER_TOKEN, TOKEN)


class FakeGh:
    def __init__(self, installed=True, logged_in=True, repo="chulgeun-ops/chulgeun-publisher",
                 set_fails=False):
        self.installed = installed
        self.logged_in = logged_in
        self.repo = repo
        self.set_fails = set_fails
        self.calls = []
        self.secrets = {}

    def which(self, name):
        return "/usr/local/bin/gh" if self.installed else None

    def __call__(self, args, stdin=None):
        args = list(args)
        self.calls.append((args, stdin))
        if args[:3] == ["gh", "auth", "status"]:
            return (0 if self.logged_in else 1), "", ""
        if args[:3] == ["gh", "api", "user"]:
            return 0, "chulgeun-ops\n", ""
        if args[:3] == ["gh", "repo", "view"]:
            return (0, self.repo + "\n", "") if self.repo else (1, "", "no git remote")
        if args[:3] == ["gh", "secret", "set"]:
            if self.set_fails:
                return 1, "", f"HTTP 404: Not Found (value {stdin})"
            self.secrets[args[3]] = (args[args.index("--repo") + 1], stdin)
            return 0, "", ""
        raise AssertionError(f"예상 못 한 gh 호출 {args}")


class SetupTokenTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGraph()
        self.gh = FakeGh()
        self.out = []
        self.answers = []
        self.secret_answers = []

    def run_setup(self, answers=None, secret_answers=None, repo=None):
        self.answers = list(answers if answers is not None else [APP_ID, "y"])
        self.secret_answers = list(secret_answers if secret_answers is not None
                                   else [APP_SECRET, SHORT_TOKEN])
        return st.run_setup(Namespace(repo=repo, version=None),
                            ask=lambda prompt: self.answers.pop(0),
                            ask_secret=lambda prompt: self.secret_answers.pop(0),
                            out=self.out.append, run=self.gh, which=self.gh.which,
                            transport=self.fake, sleep=lambda s: None, now_ts=NOW)

    def text(self):
        return "\n".join(self.out)

    def assert_no_secret_printed(self):
        t = self.text()
        for s in ALL_SECRETS:
            self.assertNotIn(s, t)
        # gh 명령줄 인자에도 비밀값이 없어야 한다(표준 입력으로만)
        for args, _ in self.gh.calls:
            for s in ALL_SECRETS:
                self.assertNotIn(s, " ".join(args))

    def test_happy_path_saves_page_token_and_user_id(self):
        rc = self.run_setup()
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.gh.secrets["FB_PAGE_ACCESS_TOKEN"],
                         ("chulgeun-ops/chulgeun-publisher", TOKEN))
        self.assertEqual(self.gh.secrets["IG_USER_ID"], ("chulgeun-ops/chulgeun-publisher", UID))
        self.assertIn("토큰 만료: 없음", self.text())
        self.assertIn("@chulgeun_munjang", self.text())
        self.assert_no_secret_printed()
        # 흐름: 교환 → 페이지 목록 → debug_token(앱 토큰으로)
        paths = [p for _, p, _ in self.fake.calls]
        self.assertEqual(paths, ["oauth/access_token", "me/accounts", "debug_token"])
        self.assertEqual(self.fake.tokens_used[2], f"{APP_ID}|{APP_SECRET}")

    def test_gh_missing(self):
        self.gh.installed = False
        self.assertEqual(self.run_setup(), 2)
        self.assertIn("brew install gh", self.text())
        self.assertEqual(self.fake.calls, [])   # Meta 호출 전에 멈춤

    def test_gh_not_logged_in(self):
        self.gh.logged_in = False
        self.assertEqual(self.run_setup(), 2)
        self.assertIn("gh auth login", self.text())
        self.assertEqual(self.fake.calls, [])

    def test_repo_prompt_when_no_remote(self):
        self.gh.repo = None
        rc = self.run_setup(answers=["ops/pub", APP_ID, "y"])
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.gh.secrets["IG_USER_ID"][0], "ops/pub")

    def test_user_declines_nothing_saved(self):
        self.assertEqual(self.run_setup(answers=[APP_ID, "n"]), 2)
        self.assertEqual(self.gh.secrets, {})

    def test_page_not_linked_to_instagram(self):
        self.fake.ig_linked = False
        self.assertEqual(self.run_setup(), 1)
        self.assertIn("SETUP.md 3단계", self.text())
        self.assertEqual(self.gh.secrets, {})

    def test_no_pages(self):
        self.fake.pages = []
        self.assertEqual(self.run_setup(), 1)
        self.assertIn("pages_show_list", self.text())

    def test_exchange_failure_redacts(self):
        self.fake.fail["exchange"] = (400, {"message": f"Error validating client secret {APP_SECRET}",
                                            "code": 101})
        self.assertEqual(self.run_setup(), 1)
        self.assertIn("교환 실패", self.text())
        self.assert_no_secret_printed()

    def test_expiring_page_token_is_not_saved(self):
        self.fake.debug_info[TOKEN]["expires_at"] = NOW + 3600
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(self.gh.secrets, {})
        self.assertIn("저장하지 않습니다", self.text())

    def test_missing_publish_permission_is_not_saved(self):
        self.fake.debug_info[TOKEN]["scopes"] = ["pages_show_list", "instagram_basic"]
        self.assertEqual(self.run_setup(), 1)
        self.assertIn("instagram_content_publish", self.text())
        self.assertEqual(self.gh.secrets, {})

    def test_multiple_linked_pages_choose(self):
        self.fake.pages = [
            {"id": "p1", "name": "다른 페이지", "access_token": "EAAother",
             "instagram_business_account": {"id": "17841499", "username": "other"}},
            {"id": PAGE_ID, "name": "출근문장", "access_token": TOKEN,
             "instagram_business_account": {"id": UID, "username": "chulgeun_munjang"}},
        ]
        rc = self.run_setup(answers=[APP_ID, "2", "y"])
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.gh.secrets["FB_PAGE_ACCESS_TOKEN"][1], TOKEN)

    def test_secret_set_failure_redacts(self):
        self.gh.set_fails = True
        self.assertEqual(self.run_setup(), 1)
        self.assertIn("저장 실패", self.text())
        self.assert_no_secret_printed()

    def test_non_numeric_app_id(self):
        self.assertEqual(self.run_setup(answers=["abc"]), 2)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
