"""症例(Case)モデルと保存・読み込み。"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field

from config import CASES_DIR, ensure_dirs  # type: ignore[import]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# 臨床情報の入力欄定義 (キー, ラベル)。任意項目。
CLINICAL_FIELDS = [
    ("site", "部位"),
    ("age_sex", "年齢・性別"),
    ("remark", "備考 (問診・経過)"),
]


@dataclass
class Case:
    id: str
    title: str = ""
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    image_path: str = ""
    notes: str = ""
    clinical: dict = field(default_factory=dict)  # {"site":..., "age_sex":..., "remark":...}
    messages: list = field(default_factory=list)

    def touch(self) -> None:
        self.updated = _now()

    def clinical_summary(self) -> str:
        """臨床情報のうち入力済みのものをプロンプト用の文章にする。"""
        lines = []
        for key, label in CLINICAL_FIELDS:
            val = (self.clinical or {}).get(key, "")
            if isinstance(val, str) and val.strip():
                lines.append(f"- {label}: {val.strip()}")
        return "\n".join(lines)

    @staticmethod
    def _first_user_text(messages: list) -> str:
        """最初のユーザー発言のテキストを返す (表示用 display を優先)。

        display がない旧形式のJSONでは content の本文を使う。所見・鑑別
        プロンプトの場合はヘッダ行 (# で始まる行など) を除いた本文を使う。
        """
        for m in messages or []:
            if not (isinstance(m, dict) and m.get("role") == "user"):
                continue
            display = (m.get("display") or "").strip()
            if display:
                return display
            content = m.get("content")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
            else:
                text = ""
            if text:
                return text
        return ""

    def auto_title(self) -> None:
        """タイトルが未設定・「(無題)」のままなら内容から自動生成する。

        優先順: 画像ファイル名 → 最初のユーザー発言 (表示用 display)。
        手動で付けたタイトル (「(無題)」以外) は決して上書きしない。
        """
        t = self.title.strip()
        if t and t != "(無題)":
            return
        if self.image_path:
            self.title = os.path.splitext(os.path.basename(self.image_path))[0]
            return
        text = self._first_user_text(self.messages)
        if text:
            # 添付注記や定型ラベルを除いてタイトルらしい形にする
            text = re.sub(r"\s*\[(画像x\d+を添付)\]", "", text)
            text = re.sub(r"^\[(所見の検討|鑑別診断の検討)\]\s*", "", text)
            # 旧形式JSONの本文からはプロンプトのヘッダ行を除く
            body_lines = [
                ln for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith(("#", "(", "※"))
            ]
            if body_lines:
                text = "\n".join(body_lines)
            self.title = text.strip()[:30] or "(無題)"
        else:
            self.title = "(無題)"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "image_path": self.image_path,
            "notes": self.notes,
            "clinical": self.clinical,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Case":
        return cls(
            id=d.get("id") or uuid.uuid4().hex[:12],
            title=d.get("title", ""),
            created=d.get("created", _now()),
            updated=d.get("updated", _now()),
            image_path=d.get("image_path", "") or "",
            notes=d.get("notes", "") or "",
            clinical=d.get("clinical") if isinstance(d.get("clinical"), dict) else {},
            messages=[m for m in d.get("messages", []) if isinstance(m, dict)],
        )

    def save(self) -> None:
        ensure_dirs()
        self.auto_title()
        self.touch()
        path = os.path.join(CASES_DIR, f"{self.id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


class CaseStore:
    def list_cases(self) -> list[dict]:
        """メタ情報の一覧を返す (作成順・新しい順で固定)。

        updated (最終保存時刻) はクリック・自動保存のたびに変わるため、
        これで並べるとクリックするだけで順番が入れ替わってしまう。
        不変の created で並べることで履歴パネルの順序は常に固定。
        """
        ensure_dirs()
        items = []
        for fn in os.listdir(CASES_DIR):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(CASES_DIR, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                items.append({
                    "id": d.get("id", os.path.splitext(fn)[0]),
                    "title": d.get("title", "(無題)"),
                    "created": d.get("created", ""),
                    "updated": d.get("updated", ""),
                    "image_path": d.get("image_path", ""),
                })
            except Exception:
                continue
        # 同秒作成の症例は id (不変) で順序を確定させる
        items.sort(key=lambda x: (x["created"], x["id"]), reverse=True)
        return items

    def load(self, case_id: str) -> Case | None:
        path = os.path.join(CASES_DIR, f"{case_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Case.from_dict(json.load(f))
        except Exception:
            return None

    def delete(self, case_id: str) -> None:
        path = os.path.join(CASES_DIR, f"{case_id}.json")
        if os.path.exists(path):
            os.remove(path)

    @staticmethod
    def new_case() -> Case:
        return Case(id=uuid.uuid4().hex[:12])
