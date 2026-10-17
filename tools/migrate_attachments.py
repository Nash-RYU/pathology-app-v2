#!/usr/bin/env python3
"""既存の症例JSONに埋め込まれたbase64添付画像を attachments/ へPNG化し、
JSONにはパス参照だけを残す移行ツール。

背景:
  かつては送信済み添付画像をbase64で症例JSONに埋め込んでいたため、
  履歴JSONが数十MBに肥大化し、開くのも遅かった。本ツールは:
  - messages 内の image_url パート (data:image/...;base64,...) を
    ~/.pathology_app/attachments/{case_id}_migr_{連番}.png へ抽出し
  - 同パートを {"type": "image_url", "image_url": {"path": ...}} に書き直す
  - 元のJSONは ~/.pathology_app/backup/cases_{日時}/ にコピーして残す

使い方 (アプリを閉じてから実行してください):
  python tools/migrate_attachments.py          # ドライラン (変更しない)
  python tools/migrate_attachments.py --write  # 実際に書き換える
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

APP_DIR = os.path.join(os.path.expanduser("~"), ".pathology_app")
CASES_DIR = os.path.join(APP_DIR, "cases")
ATTACH_DIR = os.path.join(APP_DIR, "attachments")


def _extract_base64_parts(content) -> list[dict]:
    """メッセージcontent (list) のうちbase64埋め込みのimage_urlパート一覧。"""
    out = []
    if not isinstance(content, list):
        return out
    for p in content:
        if not (isinstance(p, dict) and p.get("type") == "image_url"):
            continue
        iu = p.get("image_url") or {}
        url = iu.get("url") or ""
        if isinstance(url, str) and url.startswith("data:image/"):
            out.append(p)
    return out


def migrate_case(path: str, write: bool, attach_dir: str, backup_dir: str) -> dict:
    """症例JSON 1件を移行する。結果の統計を返す。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    case_id = data.get("id") or os.path.splitext(os.path.basename(path))[0]
    orig_size = os.path.getsize(path)
    seq = 0
    for m in data.get("messages", []):
        content = m.get("content")
        for i, p in enumerate(content or []):
            if not (isinstance(p, dict) and p.get("type") == "image_url"):
                continue
            iu = p.get("image_url") or {}
            url = iu.get("url") or ""
            if not (isinstance(url, str) and url.startswith("data:image/")):
                continue
            try:
                blob = base64.b64decode(url.split("base64,", 1)[-1])
            except Exception:
                continue
            stem = f"{case_id}_migr_{seq:03d}"
            out_path = os.path.join(attach_dir, f"{stem}.png")
            n = 1
            while os.path.exists(out_path):
                out_path = os.path.join(attach_dir, f"{stem}_{n}.png")
                n += 1
            if write:
                os.makedirs(attach_dir, exist_ok=True)
                tmp = out_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, out_path)
            content[i] = {"type": "image_url", "image_url": {"path": out_path}}
            seq += 1
    if not write or seq == 0:
        return {"id": case_id, "images": seq, "orig": orig_size,
                "new": orig_size, "written": False}
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, os.path.basename(path))
    if not os.path.exists(backup):
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    new_size = os.path.getsize(path)
    return {"id": case_id, "images": seq, "orig": orig_size,
            "new": new_size, "written": True}


def main(write: bool = False) -> int:
    cases_dir = CASES_DIR
    attach_dir = ATTACH_DIR
    backup_dir = os.path.join(
        APP_DIR, "backup", f"cases_{time.strftime('%Y%m%d-%H%M%S')}"
    )
    total_img = 0
    total_old = total_new = 0
    for fn in sorted(os.listdir(cases_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(cases_dir, fn)
        try:
            r = migrate_case(path, write, attach_dir, backup_dir)
        except Exception as e:
            print(f"SKIP {fn}: {e}")
            continue
        total_img += r["images"]
        total_old += r["orig"]
        total_new += r["new"]
        print(
            f"case {r['id']}: base64画像 {r['images']}件  "
            f"{r['orig'] / 1e6:.1f}MB -> {r['new'] / 1e6:.1f}MB"
            + ("" if r["written"] else "  (ドライラン)")
        )
    print(
        f"\n合計: 画像 {total_img}件  "
        f"{total_old / 1e6:.1f}MB -> {total_new / 1e6:.1f}MB"
        + ("  バックアップ: " + backup_dir if write else "  (--write で実際に書き換え)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(write="--write" in sys.argv))
