"""큐 준비 도구 테스트. sips 변환 테스트는 macOS 에서만 실행된다."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zlib
import struct
from argparse import Namespace
from pathlib import Path

import helpers  # noqa: F401  (scripts 경로 등록)
import queue_lib as ql
import queue_tool as qt

SERIES = """# 시리즈

## 1/2 첫 편 제목

### 카드 문구

| 장 | 역할 | 문구 | 비고 |
|---|---|---|---|
| 1 | 표지(훅) | **제목**: 결과가 / 안 좋은 주,<br>**메모지**: 일 읽기 1/2 | 비고 |
| 2 | 예를 들면 | **예를 들면**<br>[노란 메모지] **조건 칸부터**<br>· 자료가 늦었다<br>　들여쓰기 | |

### 캡션

```
첫 줄 "따옴표"
둘째 줄

#직장인 #직장인공감 #슬럼프 #총균쇠 #출근문장
```

- 첫 문단 약 85자.

---

## 2/2 둘째 편

### 카드 문구

| 장 | 역할 | 문구 | 비고 |
|---|---|---|---|
| 1 | 표지 | 둘째 표지 | |
| 2 | 정리 | 둘째 정리 | |

### 캡션

```
둘째 캡션
#직장인 #문제해결 #직장인공감 #총균쇠 #출근문장
```
"""


def png_bytes(w, h):
    """단색 RGB PNG(무압축에 가까운 최소 구현)."""
    raw = b"".join(b"\x00" + b"\xff\xe4\x5c" * w for _ in range(h))

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class ParseTest(unittest.TestCase):
    def test_section_caption_alt(self):
        title, sec = qt.extract_section(SERIES, 1)
        self.assertEqual(title, "1/2 첫 편 제목")
        self.assertEqual(qt.extract_caption(sec),
                         '첫 줄 "따옴표"\n둘째 줄\n\n#직장인 #직장인공감 #슬럼프 #총균쇠 #출근문장')
        alts = qt.extract_alt_texts(sec)
        self.assertEqual(alts[0], "1/2 표지(훅). 제목: 결과가 안 좋은 주, 메모지: 일 읽기 1/2")
        self.assertEqual(alts[1], "2/2 예를 들면. 예를 들면 조건 칸부터 · 자료가 늦었다 들여쓰기")

    def test_second_section_isolated(self):
        _, sec = qt.extract_section(SERIES, 2)
        self.assertEqual(qt.extract_caption(sec).splitlines()[0], "둘째 캡션")

    def test_missing_section(self):
        with self.assertRaises(qt.ToolError):
            qt.extract_section(SERIES, 3)


class AddApproveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.q = self.root / "queue"
        self.q.mkdir()
        self.series = self.root / "series.md"
        self.series.write_text(SERIES, encoding="utf-8")
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, images=None, at="2026-11-09 07:17", slug="2026-11-09-t-1"):
        args = Namespace(series=str(self.series), part=1, images=images, slug=slug,
                         at=at, quality=90)
        return qt.cmd_add(args, queue_dir=self.q, log=self.logs.append)

    def test_add_without_images_hangs_paths(self):
        self.assertEqual(self.add(images=str(self.root / "nope")), 0)
        data = json.loads((self.q / "2026-11-09-t-1" / "post.json").read_text())
        self.assertEqual([i["file"] for i in data["images"]], ["01.jpg", "02.jpg"])
        self.assertFalse(data["approved"])
        self.assertFalse(data["images_ready"])
        self.assertEqual(data["scheduled_at"], "2026-11-09T07:17+09:00")
        # 이미지 없으면 승인 불가
        with self.assertRaises(ql.QueueError):
            qt.cmd_approve(Namespace(slug="2026-11-09-t-1"), queue_dir=self.q, log=self.logs.append)

    def test_bad_slug(self):
        with self.assertRaises(qt.ToolError):
            self.add(slug="총균쇠")

    def test_refuses_posted_item(self):
        self.add()
        (self.q / "2026-11-09-t-1" / "posted.json").write_text('{"media_id": "m"}')
        with self.assertRaises(qt.ToolError):
            self.add()

    @unittest.skipUnless(shutil.which("sips"), "sips 없음(macOS 전용)")
    def test_convert_approve_and_reset_on_change(self):
        src = self.root / "png"
        src.mkdir()
        for n in ("01", "02"):
            (src / f"{n}.png").write_bytes(png_bytes(1080, 1350))
        self.assertEqual(self.add(images=str(src)), 0)
        item_dir = self.q / "2026-11-09-t-1"
        self.assertEqual(ql.jpeg_size(item_dir / "01.jpg"), (1080, 1350))
        qt.cmd_approve(Namespace(slug="2026-11-09-t-1"), queue_dir=self.q, log=self.logs.append)
        self.assertTrue(json.loads((item_dir / "post.json").read_text())["approved"])

        # 같은 내용으로 다시 돌리면 승인 유지
        self.add(images=str(src))
        self.assertTrue(json.loads((item_dir / "post.json").read_text())["approved"])

        # 시각이 바뀌면 승인 해제
        self.add(images=str(src), at="2026-11-10 07:17")
        self.assertFalse(json.loads((item_dir / "post.json").read_text())["approved"])

    @unittest.skipUnless(shutil.which("sips"), "sips 없음(macOS 전용)")
    def test_wrong_png_size_rejected(self):
        src = self.root / "png"
        src.mkdir()
        (src / "01.png").write_bytes(png_bytes(1080, 1080))
        with self.assertRaises(qt.ToolError):
            self.add(images=str(src))


if __name__ == "__main__":
    unittest.main()
