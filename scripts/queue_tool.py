#!/usr/bin/env python3
"""큐 준비 도구(로컬 맥에서 에이전트가 실행).

  # 시리즈 문서의 n번째 편으로 큐 항목 만들기(PNG 가 있으면 JPEG 로 변환)
  python3 scripts/queue_tool.py add --series ../content/guns-germs-steel-series.md --part 1 \
      --images ../design/export/ggs-1 --slug 2026-11-16-ggs-1 --at "2026-11-16 07:17"

  python3 scripts/queue_tool.py approve 2026-11-16-ggs-1   # 대표님 OK 후에만
  python3 scripts/queue_tool.py list
  python3 scripts/queue_tool.py validate   # CI: 미게시 항목 검사

- 캡션은 시리즈 문서의 '### 캡션' 코드 블록을 글자 그대로 가져온다.
- alt_text 는 '### 카드 문구' 표의 '문구' 칸에서 만든다.
- 내용(캡션·이미지)이 바뀌면 승인(approved)을 자동으로 해제한다.
- 이미 게시된 항목(posted.json 있음)은 절대 고치지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_lib as ql  # noqa: E402

JPEG_QUALITY = 90


class ToolError(Exception):
    pass


# ------------------------------------------------------------------ 시리즈 문서 파싱

def extract_section(md: str, part: int) -> tuple:
    """'## {part}/N 제목' 부터 다음 '## ' 전까지. (제목, 본문)"""
    lines = md.splitlines()
    start = None
    title = ""
    for i, line in enumerate(lines):
        m = re.match(rf"^##\s+({part}/\d+\s+.*)$", line)
        if m:
            start, title = i, m.group(1).strip()
            break
    if start is None:
        raise ToolError(f"시리즈 문서에서 '## {part}/…' 제목을 찾지 못했습니다")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s", lines[j]):
            end = j
            break
    return title, "\n".join(lines[start + 1:end])


def extract_caption(section: str) -> str:
    m = re.search(r"^###\s*캡션\s*$\n+```[^\n]*\n(.*?)\n```", section, re.M | re.S)
    if not m:
        raise ToolError("'### 캡션' 아래 ``` 코드 블록을 찾지 못했습니다")
    return m.group(1).strip("\n")


def clean_cell(text: str) -> str:
    t = text.replace("<br>", "\n")
    t = t.replace("**", "").replace("`", "")
    t = re.sub(r"\[(노란 메모지|회색 점선 상자)[^\]]*\]\s*", "", t)
    t = t.replace("　", " ")
    t = re.sub(r" / ", " ", t)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in t.split("\n")]
    return " ".join(ln for ln in lines if ln)


def extract_alt_texts(section: str) -> List[str]:
    m = re.search(r"^###\s*카드 문구\s*$(.*?)(?=^###\s|\Z)", section, re.M | re.S)
    if not m:
        raise ToolError("'### 카드 문구' 표를 찾지 못했습니다")
    rows = []
    for line in m.group(1).splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.fullmatch(r"\d+(-\d+)?", cells[0]):
            continue
        rows.append((cells[1], clean_cell(cells[2])))
    total = len(rows)
    return [f"{i}/{total} {role}. {text}" for i, (role, text) in enumerate(rows, 1)]


# ------------------------------------------------------------------ 이미지

def convert_png(src: Path, dst: Path, quality: int = JPEG_QUALITY) -> None:
    if shutil.which("sips") is None:
        raise ToolError("맥의 sips 명령이 없습니다(이 도구는 macOS 에서 실행)")
    res = subprocess.run(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality), str(src), "--out", str(dst)],
        capture_output=True, text=True)
    if res.returncode != 0 or not dst.exists():
        raise ToolError(f"{src.name} 변환 실패: {res.stderr.strip() or res.stdout.strip()}")
    w, h = ql.jpeg_size(dst)
    if (w, h) != ql.EXPECTED_SIZE:
        dst.unlink()
        raise ToolError(f"{src.name}: 크기 {w}x{h}. 원본을 {ql.EXPECTED_SIZE[0]}x{ql.EXPECTED_SIZE[1]}로 다시 뽑아 주세요")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ 명령

def parse_kst(value: str) -> str:
    v = value.strip().replace(" ", "T")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ql.KST)
    return dt.astimezone(ql.KST).isoformat(timespec="minutes")


def cmd_add(args, queue_dir: Path = ql.QUEUE_DIR, log=print) -> int:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-[a-z0-9-]+", args.slug):
        raise ToolError("slug 는 'YYYY-MM-DD-영문소문자' 형식이어야 합니다(예: 2026-11-16-ggs-1)")
    md = Path(args.series).read_text(encoding="utf-8")
    title, section = extract_section(md, args.part)
    caption = extract_caption(section)
    alts = extract_alt_texts(section)
    scheduled = parse_kst(args.at)

    item_dir = queue_dir / args.slug
    old: Optional[dict] = None
    if (item_dir / ql.POSTED_FILE).exists():
        raise ToolError(f"{args.slug} 는 이미 게시되었습니다. 고치지 않습니다")
    if (item_dir / ql.POST_FILE).exists():
        old = json.loads((item_dir / ql.POST_FILE).read_text(encoding="utf-8"))
    item_dir.mkdir(parents=True, exist_ok=True)

    src_dir = Path(args.images) if args.images else None
    pngs = sorted(src_dir.glob("[0-9][0-9].png")) if src_dir and src_dir.exists() else []
    images: List[Dict] = []
    if pngs:
        with tempfile.TemporaryDirectory() as tmp:
            converted = []
            for png in pngs:
                out = Path(tmp) / (png.stem + ".jpg")
                convert_png(png, out, args.quality)
                converted.append(out)
            for old_jpg in item_dir.glob("*.jpg"):
                old_jpg.unlink()
            for i, out in enumerate(converted):
                dst = item_dir / out.name
                shutil.move(str(out), dst)
                images.append({"file": dst.name,
                               "alt_text": alts[i] if i < len(alts) else "",
                               "sha256": sha256(dst)})
        log(f"이미지 {len(images)}장 JPEG 변환(품질 {args.quality})")
        if len(pngs) != len(alts):
            log(f"경고: PNG {len(pngs)}장, 카드 문구 표 {len(alts)}행 — 장수가 다릅니다. alt_text 를 확인하세요")
    else:
        images = [{"file": f"{i:02d}.jpg", "alt_text": a, "sha256": None} for i, a in enumerate(alts, 1)]
        log(f"경고: PNG 가 아직 없습니다({src_dir}). 이미지 {len(images)}장 경로만 큐에 걸어 둡니다")

    data = {
        "schema": 1,
        "slug": args.slug,
        "title": title,
        "scheduled_at": scheduled,
        "approved": False,
        "images_ready": bool(pngs),
        "caption": caption,
        "images": images,
        "source": {"series": Path(args.series).name, "part": args.part},
    }
    if old:
        same = (old.get("caption") == caption and old.get("images") == images
                and old.get("scheduled_at") == scheduled)
        if old.get("approved") and same:
            data["approved"] = True
            data["approved_at"] = old.get("approved_at")
        elif old.get("approved"):
            log("알림: 내용이 바뀌어 승인을 해제했습니다. 대표님 확인 후 다시 approve 하세요")
        if old.get("skip"):
            data["skip"] = old["skip"]

    (item_dir / ql.POST_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    item = ql.load_item(item_dir)
    for w in ql.validate_item(item, require_images=False):
        log(f"경고: {w}")
    log(f"큐 항목 저장: queue/{args.slug}/post.json ({scheduled}, 승인 {'됨' if data['approved'] else '대기'})")
    return 0


def cmd_approve(args, queue_dir: Path = ql.QUEUE_DIR, log=print) -> int:
    item = ql.load_item(queue_dir / args.slug)
    if item.is_posted:
        raise ToolError(f"{args.slug} 는 이미 게시되었습니다")
    ql.validate_item(item, require_images=True)  # 이미지 없으면 승인 불가
    item.data["approved"] = True
    item.data["approved_at"] = datetime.now(tz=ql.KST).isoformat(timespec="minutes")
    (item.path / ql.POST_FILE).write_text(json.dumps(item.data, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    log(f"승인: {args.slug} ({item.data['scheduled_at']} 게시 예정)")
    return 0


def cmd_list(args, queue_dir: Path = ql.QUEUE_DIR, log=print) -> int:
    for it in ql.load_queue(queue_dir):
        if it.is_posted:
            state = f"게시됨 {it.posted.get('permalink') or it.posted.get('media_id')}"
        else:
            state = ("승인됨" if it.approved else "승인 대기") + \
                    (", 이미지 준비" if ql.images_ready(it) else ", 이미지 없음") + \
                    (", 건너뜀(skip)" if it.data.get("skip") else "")
        log(f"{it.scheduled_at:%Y-%m-%d(%a) %H:%M} {it.slug:<22} {state}")
    return 0


def cmd_validate(args, queue_dir: Path = ql.QUEUE_DIR, log=print) -> int:
    """게시 안 된 항목 전체 검사(CI 용). 승인된 항목은 이미지까지 확인."""
    bad = 0
    for it in ql.load_queue(queue_dir):
        if it.is_posted or it.data.get("skip"):
            continue
        try:
            for w in ql.validate_item(it, require_images=it.approved):
                log(f"경고 {it.slug}: {w}")
            log(f"OK {it.slug}")
        except ql.QueueError as e:
            bad += 1
            log(f"오류 {e}")
    return 1 if bad else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="출근문장 게시 큐 준비 도구")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--series", required=True)
    a.add_argument("--part", type=int, required=True)
    a.add_argument("--images")
    a.add_argument("--slug", required=True)
    a.add_argument("--at", required=True, help="게시 시각 KST, 예: '2026-11-09 07:17'")
    a.add_argument("--quality", type=int, default=JPEG_QUALITY)
    ap = sub.add_parser("approve")
    ap.add_argument("slug")
    sub.add_parser("list")
    sub.add_parser("validate")
    args = p.parse_args(argv)
    try:
        return {"add": cmd_add, "approve": cmd_approve, "list": cmd_list,
                "validate": cmd_validate}[args.cmd](args)
    except (ToolError, ql.QueueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
