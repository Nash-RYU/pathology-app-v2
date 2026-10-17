#!/usr/bin/env python3
"""添付PNG保存 + 履歴表示 + 履歴パネル一覧のテスト (オフスクリーン)。"""
import io
import json
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "pathology_app"))
sys.path.insert(0, os.path.join(_here, ".."))

try:
    import openslide  # noqa: F401  (PySide6より先にロードする規約)
except Exception:
    openslide = None

from PIL import Image  # noqa: E402
from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtGui import QTextDocument  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from chat import ChatPanel, _data_url_png  # noqa: E402
from history import HistoryPanel  # noqa: E402

app = QApplication([])

# --- テスト用の小さなPNGを作る ---------------------------------------------
buf = io.BytesIO()
Image.new("RGB", (320, 240), (90, 140, 200)).save(buf, "PNG")
png_bytes = buf.getvalue()
assert png_bytes[:4] == b"\x89PNG"


class FakeClient:
    def stream_chat(self, messages):
        yield "OK"


def png_data_url(data=png_bytes):
    import base64
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


# --- 1. 送信時に attachments/ へPNG保存・メッセージはパス参照 -------------
chat = ChatPanel()
chat.set_llm_client(FakeClient())
chat.set_case_id("unittest")
captured = []
chat._start_worker = lambda msgs: captured.append(msgs)

# 保留中の添付を直接積む (「＋ 画像…」相当)
from PySide6.QtGui import QPixmap  # noqa: E402
thumb = QPixmap(72, 72)
chat._pending_images.append({"path": "/tmp/foo.png", "png": png_bytes,
                             "thumb": thumb})
chat.set_context_getter(lambda: {"case_info": "x", "notes": "", "png": None})
chat.input.setPlainText("画像を見て")
chat._send_user_text()

att_files = [f for f in os.listdir(config.ATTACHMENTS_DIR)
             if f.startswith("unittest_")]
assert len(att_files) == 1, f"添付PNGが1件でない: {att_files}"
saved_path = os.path.join(config.ATTACHMENTS_DIR, att_files[0])
with open(saved_path, "rb") as f:
    assert f.read(4) == b"\x89PNG", "保存された添付がPNGでない"
user_msg = chat.messages[0]
img_part = next(p for p in user_msg["content"] if p.get("type") == "image_url")
assert img_part["image_url"]["path"] == saved_path, "パス参照が保存されていません"
assert "base64" not in json.dumps(user_msg), "メッセージにbase64が残っています"
print("OK: 送信時にPNG保存・メッセージはパス参照")

# --- 2. LLMペイロードはbase64 data URLに変換される --------------------------
base_msgs = chat._llm_messages(chat.messages)
last = base_msgs[-1]
assert any(
    isinstance(p, dict) and p.get("type") == "image_url"
    and (p.get("image_url") or {}).get("url", "").startswith("data:image/png;base64,")
    for p in last["content"]
), "LLMペイロードに画像が含まれません"
print("OK: LLMペイロードのbase64変換")

# --- 3. 履歴JSONに保存しても小さい・base64ゼロ ------------------------------
payload = json.dumps(chat.messages, ensure_ascii=False)
assert "base64" not in payload and len(payload) < 4096, "保存JSONが大きすぎる"
print(f"OK: 保存JSONは軽量 ({len(payload)}バイト)")

# --- 4. 過去症例を開いたとき添付サムネイルが表示される ---------------------
chat2 = ChatPanel()
chat2.load_messages(chat.messages)
res = chat2.browser.document().resource(
    int(QTextDocument.ResourceType.ImageResource), QUrl("attachimg0")
)
assert res is not None and not res.isNull(), "サムネイルリソースが登録されていません"
assert "attachimg0" in chat2.browser.toHtml(), "imgタグが挿入されていません"
print("OK: 過去症例の添付サムネイル表示 (パス参照)")

# 旧形式 (base64埋め込み) の履歴も表示できること
legacy = [{"role": "user", "content": [
    {"type": "text", "text": "旧形式"},
    {"type": "image_url", "image_url": {"url": png_data_url()}},
]}]
chat3 = ChatPanel()
chat3.load_messages(legacy)
res3 = chat3.browser.document().resource(
    int(QTextDocument.ResourceType.ImageResource), QUrl("attachimg0")
)
assert res3 is not None and not res3.isNull(), "旧形式(base64)の表示に失敗"
assert _data_url_png(png_data_url()) == png_bytes, "data URLのデコードに失敗"
print("OK: 旧形式 (base64埋め込み) の履歴もサムネイル表示")

# ファイルが欠損したパス参照は「見つかりません」と出す
missing = [{"role": "user", "content": [
    {"type": "text", "text": "欠損"},
    {"type": "image_url", "image_url": {"path": "/nonexistent/x.png", "label": "消えた"}},
]}]
chat4 = ChatPanel()
chat4.load_messages(missing)
assert "見つかりません" in chat4.browser.toHtml(), "欠損表示に失敗"
print("OK: 欠損添付の案内表示")

# --- 5. 履歴パネルの「保存済み添付」一覧 -----------------------------------
h = HistoryPanel()
h.show_attachments([{"path": saved_path, "label": "スナップ1 (100x)"},
                    {"path": "/nonexistent/y.png", "label": "消えた"}])
assert h.att_list.count() == 2, "添付一覧の件数が不正"
assert "(ファイルなし)" in h.att_list.item(1).text(), "欠損表示に失敗"
assert h.att_list.item(0).text() == "スナップ1 (100x)"
assert not h.att_list.item(0).icon().isNull(), "サムネイルアイコンが設定されていません"
print("OK: 履歴パネルの保存済み添付一覧")

# --- 6. 症例JSONを実際に保存・再読み込み -----------------------------------
from case import Case  # noqa: E402

case = Case(id="unittest", title="添付テスト")
case.messages = chat.messages
case.save()
loaded = json.load(open(os.path.join(config.CASES_DIR, "unittest.json"),
                        encoding="utf-8"))
assert "base64" not in json.dumps(loaded, ensure_ascii=False), "症例JSONにbase64が残っています"
print("OK: 症例JSONの保存 (パス参照のみ)")

# --- 7. 移行ツール (一時ディレクトリで) ------------------------------------
import tempfile  # noqa: E402
from tools.migrate_attachments import migrate_case  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    legacy_json = {
        "id": "migr01",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "移行前"},
            {"type": "image_url", "image_url": {"url": png_data_url()}},
        ]}],
    }
    src = os.path.join(td, "migr01.json")
    with open(src, "w", encoding="utf-8") as f:
        json.dump(legacy_json, f)
    attach_dir = os.path.join(td, "attachments")
    backup_dir = os.path.join(td, "backup")
    # ドライラン: 書き換えない
    r = migrate_case(src, False, attach_dir, backup_dir)
    assert r["images"] == 1 and not r["written"], "ドライラン統計が不正"
    assert os.path.exists(src) and not os.path.exists(attach_dir), "ドライランで書き換えた"
    # 書き込み
    r = migrate_case(src, True, attach_dir, backup_dir)
    assert r["written"], "移行書き込みに失敗"
    written = json.load(open(src, encoding="utf-8"))
    part = written["messages"][0]["content"][1]
    new_path = part["image_url"]["path"]
    assert os.path.exists(new_path), "抽出PNGが存在しない"
    with open(new_path, "rb") as f:
        assert f.read() == png_bytes, "抽出PNGの内容が一致しません"
    assert os.path.exists(os.path.join(backup_dir, "migr01.json")), "バックアップがありません"
    # 再読み込み表示
    chat5 = ChatPanel()
    chat5.load_messages(written["messages"])
    res5 = chat5.browser.document().resource(
        int(QTextDocument.ResourceType.ImageResource), QUrl("attachimg0")
    )
    assert res5 is not None and not res5.isNull(), "移行後の表示に失敗"
    print("OK: 移行ツール (base64 → PNG抽出 + パス参照 + バックアップ)")

# --- 後片付け ---------------------------------------------------------------
for p in (saved_path, saved_path + ".tmp",
          os.path.join(config.CASES_DIR, "unittest.json")):
    if os.path.exists(p):
        os.remove(p)
print("ATTACH_TEST_OK")
