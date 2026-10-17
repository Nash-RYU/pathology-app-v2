#!/usr/bin/env python3
"""自動タイトル生成 + リネーム機能のテスト (オフスクリーン)。"""
import json
import os
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "pathology_app"))
sys.path.insert(0, os.path.join(_here, ".."))

os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    import openslide  # noqa: F401  (PySide6より先にロードする規約)
except Exception:
    openslide = None

from unittest.mock import patch  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from case import Case  # noqa: E402
from history import HistoryPanel  # noqa: E402

app = QApplication([])

# --- 1. Case.auto_title: 表示用displayからの自動生成 ------------------------
c = Case(id="titletest1", title="")
c.messages = [
    {"role": "user",
     "content": [{"type": "text", "text": "胃生検を評価してください"}],
     "display": "胃生検を評価してください"},
    {"role": "assistant", "content": "胃粘膜に慢性炎細胞浸潤があります…"},
]
c.auto_title()
assert c.title == "胃生検を評価してください", c.title
print("OK: 最初のユーザー発言から自動タイトル")

# 「(無題)」のままの症例も再生成対象
c2 = Case(id="titletest2", title="(無題)")
c2.messages = [
    {"role": "user", "content": "頸部リンパ節の評価です"},
    {"role": "assistant", "content": "…"},
]
c2.auto_title()
assert c2.title == "頸部リンパ節の評価です", c2.title
print("OK: 「(無題)」症例の再生成")

# 所見プロンプト (displayあり) はラベルと添付注記を外す
c3 = Case(id="titletest3", title="")
c3.messages = [
    {"role": "user", "content": "所見の検討", "display":
     "[所見の検討] 補足: リンパ節検体 [画像x2を添付]"},
    {"role": "assistant", "content": "…"},
]
c3.auto_title()
assert c3.title == "補足: リンパ節検体", c3.title
print("OK: 所見プロンプトのdisplayからラベル除去")

# 旧形式 (displayなし、プロンプト本文) はヘッダ行を除いた本文から
c4 = Case(id="titletest4", title="(無題)")
c4.messages = [
    {"role": "user", "content":
     "# 標本情報\n(情報なし)\n\n# ユーザーメモ\n(なし)\n\n"
     "この標本について、気づいた所見を箇条書きで列挙してください。"},
    {"role": "assistant", "content": "…"},
]
c4.auto_title()
assert c4.title.startswith("この標本について"), c4.title
print("OK: 旧形式プロンプト本文からのタイトル")

# 手動タイトルは上書きしない
c5 = Case(id="titletest5", title="医師が付けた名前")
c5.messages = [{"role": "user", "content": "別の内容"},
               {"role": "assistant", "content": "…"}]
c5.auto_title()
assert c5.title == "医師が付けた名前"
print("OK: 手動タイトルの保護")

# メッセージがない・発言がない場合は (無題) のまま
c6 = Case(id="titletest6", title="(無題)")
c6.auto_title()
assert c6.title == "(無題)"
print("OK: 内容がない場合は (無題)")

# --- 2. タイトル整形 -------------------------------------------------------
from main import _truncate_title  # noqa: E402

assert _truncate_title("「胃粘膜の慢性炎」") == "胃粘膜の慢性炎"
assert _truncate_title("タイトル: 肝の転移巣") == "肝の転移巣"
assert _truncate_title("Title: \"Metastasis\"\n解説文") == "Metastasis"
assert _truncate_title("") == ""
print("OK: LLM出力のタイトル整形")

# --- 3. MainWindow でのLLM自動タイトル ------------------------------------
from main import MainWindow  # noqa: E402


class FakeTitleClient:
    def stream_chat(self, messages):
        yield "「胃粘膜の慢性炎」"


created_cases: list[str] = []


def make_window():
    win = MainWindow()
    created_cases.append(win.case.id)
    return win


win = make_window()
win.client = FakeTitleClient()
win.case.title = "(無題)"
win.chat.messages = [
    {"role": "user",
     "content": [{"type": "text", "text": "胃生検を評価してください"}],
     "display": "胃生検を評価してください"},
    {"role": "assistant", "content": "胃粘膜に中等度の慢性炎細胞浸潤があります"},
]
win._maybe_generate_title()

deadline = time.time() + 15
while time.time() < deadline and win.case.title != "胃粘膜の慢性炎":
    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()
assert win.case.title == "胃粘膜の慢性炎", win.case.title
assert not win._title_is_auto, "LLMタイトル後もauto扱いのまま"
print("OK: LLMによる自動タイトル適用")

# 確定したLLMタイトルは再生成しない
win._maybe_generate_title()
assert win._title_worker is None or not win._title_worker.isRunning()
print("OK: LLMタイトル確定後の再生成なし")

# --- 4. 手動リネーム ------------------------------------------------------
win2 = make_window()
win2.client = FakeTitleClient()
win2.case.title = "(無題)"
win2.chat.messages = [
    {"role": "user", "content": "何か質問"},
    {"role": "assistant", "content": "回答"},
]
win2._maybe_generate_title()
deadline = time.time() + 15
while time.time() < deadline and win2.case.title == "(無題)":
    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()
assert win2.case.title == "胃粘膜の慢性炎"

# リネーム → 保護される (LLMでも再生成しない)
win2._rename_case(win2.case.id, "医師が命名")
assert win2.case.title == "医師が命名"
assert not win2._title_is_auto
win2._maybe_generate_title()
deadline = time.time() + 8
while time.time() < deadline and win2.case.title != "胃粘膜の慢性炎":
    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()
assert win2.case.title == "医師が命名", win2.case.title
print("OK: 手動リネームはLLM再生成でも保護")

# 別の症例 (現在のcaseでない) のリネーム
other = Case(id="titletest7", title="(無題)")
other.save()
created_cases.append(other.id)
win2._rename_case(other.id, "リネームされた症例")
reloaded = win2.store.load(other.id)
assert reloaded.title == "リネームされた症例"
print("OK: 現在の症例でないもののリネーム")

# --- 5. 履歴パネルのリネームシグナル --------------------------------------
h = HistoryPanel()
sent = []
h.rename_case.connect(lambda cid, t: sent.append((cid, t)))
with patch("history.QInputDialog.getText", return_value=("新しい名前", True)):
    h.refresh([{"id": "titletest7", "title": "リネームされた症例",
                "updated": "", "image_path": ""}])
    h.list.setCurrentRow(0)
    h._emit_rename()
assert sent == [("titletest7", "新しい名前")], sent
print("OK: 履歴パネルの名前変更シグナル")

# --- 後片付け -------------------------------------------------------------
for cid in created_cases:
    p = os.path.join(config.CASES_DIR, f"{cid}.json")
    if os.path.exists(p):
        os.remove(p)
for cid in ("titletest1", "titletest2", "titletest3", "titletest4",
            "titletest5", "titletest6", "titletest7"):
    p = os.path.join(config.CASES_DIR, f"{cid}.json")
    if os.path.exists(p):
        os.remove(p)

sys.stdout.flush()
os._exit(0)
