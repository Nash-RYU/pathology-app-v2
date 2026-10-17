"""オフスクリーン・スモークテスト: 環境の検証用 (CIでは未使用)。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pathology_app"))

# アプリ起動時と同じ規約: OpenSlideをPySide6より先にロードする
try:
    import openslide  # noqa: F401,E402
except Exception:
    openslide = None  # type: ignore[assignment]

import random  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402
import tifffile  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from llm import LLMClient, resolve_api_key  # noqa: E402

app = QApplication([])
from main import MainWindow  # noqa: E402

win = MainWindow()
win.resize(1440, 900)

# --- 通常画像 --------------------------------------------------------------
random.seed(42)
img = Image.new("RGB", (1600, 1200), (222, 184, 196))
d = ImageDraw.Draw(img)
for _ in range(600):
    x, y = random.randint(0, 1599), random.randint(0, 1199)
    r = random.randint(4, 26)
    col = (random.randint(120, 220), random.randint(60, 140), random.randint(120, 190))
    d.ellipse((x, y, x + r, y + r), fill=col)
img.save("/tmp/test_case.png")

win.viewer.load("/tmp/test_case.png")
doc = win.viewer.doc
assert doc is not None and doc.width == 1600 and not doc.is_wsi, "通常画像の読み込みに失敗"
png = win.viewer.export_current_view(512)
assert png is not None and png[:4] == b"\x89PNG", "ビュー書き出しに失敗"
print("OK: 通常画像読み込み + ビュー書き出し")

# --- ピラミッドTIFF (openslide generic-tiff) -------------------------------
levels = []
base = 4096
for lv in range(3):
    size = base >> lv
    arr = None
    import numpy as np
    rng = np.random.default_rng(7)
    arr = rng.integers(60, 230, size=(size, size, 3), dtype="uint8")
    levels.append(arr)
with tifffile.TiffWriter("/tmp/test_pyramid.tif") as t:
    for i, lv in enumerate(levels):
        t.write(lv, photometric="rgb", tile=(256, 256),
                subfiletype=0 if i == 0 else 1)
win.viewer.load("/tmp/test_pyramid.tif")
doc = win.viewer.doc
assert doc is not None and doc.is_wsi, f"WSI読み込みに失敗 (openslide={bool(doc and doc.slide)})"
assert doc.width == 4096, f"寸法が想定外: {doc.width}"
png = win.viewer.export_current_view(512)
assert png is not None and png[:4] == b"\x89PNG", "WSIビュー書き出しに失敗"
print(f"OK: WSI(ピラミッドTIFF)読み込み levels={doc.level_ds}")

# --- 症例保存 --------------------------------------------------------------
win.notes.setPlainText("スモークテスト用メモ")
win._save_case()
assert win.case.id and os.path.exists(
    os.path.join(config.CASES_DIR, f"{win.case.id}.json")
), "症例保存に失敗"
loaded = win.store.load(win.case.id)
assert loaded is not None and loaded.notes == "スモークテスト用メモ"
assert loaded.image_path == "/tmp/test_pyramid.tif"
print("OK: 症例の保存・読み込み")

# --- LLM疎通 ---------------------------------------------------------------
settings = config.load_settings()
key = resolve_api_key(settings)
assert key, "APIキーが解決できません"
client = LLMClient(settings)
models = client.list_models()
print(f"OK: エンドポイント疎通 モデル数={len(models)}")
_base = settings["model"].rsplit("/", 1)[-1].split(":")[0]
assert any(
    _base == m.rsplit("/", 1)[-1].split(":")[0] for m in models
), f"既定モデル {settings['model']} が一覧にありません ({models[:3]}...)"

pieces = []
for piece in client.stream_chat(
    [{"role": "user", "content": "テスト配信です。『OK』とだけ返してください。"}]
):
    pieces.append(piece)
text = "".join(pieces)
assert text.strip(), "LLM応答が空です"
print(f"OK: ストリーミング応答 例: {text[:40]!r}")

# --- モデル切替バー / ハーネス登録ID / IDバリアント --------------------------
from modelbar import ModelBar  # noqa: E402

bar = ModelBar()
bar.set_models(["b", "a", "b", "unsloth/GLM-5.3-Flash-GGUF"])
assert bar.combo.count() == 3, "重複除去に失敗"
bar.set_selected("a")
assert bar.selected() == "a", "選択の維持に失敗"
bar.set_selected("x")
assert bar.selected() == "x", "未知モデルの挿入に失敗"

harness_ids = config.harness_model_ids()
assert any("glm" in i.lower() for i in harness_ids), f"ハーネス登録IDの取得に失敗: {harness_ids}"
assert any("wen" in i for i in harness_ids), f"Qwen登録IDの取得に失敗: {harness_ids}"
variants = LLMClient(config.load_settings())._model_variants()
assert len(variants) >= 2, f"バリアント生成に失敗: {variants}"
print(f"OK: モデルバー / ハーネス登録ID ({len(harness_ids)}件) / バリアント {variants}")

# --- WSIリージョン計算 (パン修正: read_regionはlevel-0座標基準) -------------
from PySide6.QtCore import QPointF  # noqa: E402

win.viewer.resize(800, 600)
doc = win.viewer.doc
assert doc is not None and doc.is_wsi, "WSIが読み込まれていません"
win.viewer._fit()
# 中心をオフセットしてズームし、level>=1 が選ばれる状態を作る
win.viewer.center = QPointF(doc.width * 0.75, doc.height * 0.25)
win.viewer.zoom = 0.3
region = win.viewer._region_for_view()
assert region is not None, "リージョン計算に失敗"
x0, y0, level, rw, rh = region
vis = win.viewer._visible_slide_rect(800, 600)
ds = doc.level_ds[level]
# x0 は「レベル-0座標」で可視領域左上と一致すべき (旧バグ: vis/ds を渡していた)
assert abs(x0 - vis.left()) <= ds + 1, f"x0={x0} が level-0基準の可視左端 {vis.left()} と乖離"
assert abs(y0 - vis.top()) <= ds + 1, f"y0={y0} が level-0基準の可視上端 {vis.top()} と乖離"
qimg = doc.read_region(x0, y0, level, rw, rh)
assert qimg.width() == rw and qimg.height() == rh, "読み出しサイズ不一致"
print(f"OK: WSIリージョン計算 (level={level}, ds={ds}, x0={x0})")

# --- チャット送信内容 (自由入力の添付 / 所見ボタンの補足反映) ---------------
captured = []
win.chat._start_worker = lambda msgs: captured.append(msgs)  # worker起動をスタブ化

# 自由入力 + 画像添付チェック → image_url部分が含まれること
win.chat.chk_attach.setChecked(True)
win.chat.set_context_getter(lambda: {"case_info": "x", "notes": "", "png": b"\x89PNGdummy"})
win.chat.input.setPlainText("自由に質問")
win.chat._send_user_text()
last = captured[-1][-1]["content"]
assert isinstance(last, list) and any(
    p.get("type") == "image_url" for p in last
), "自由入力の画像添付が含まれません"

# 所見ボタン + 入力欄テキスト → 補足指示としてプロンプトに含まれること
win.chat.chk_attach.setChecked(False)
win.chat.input.setPlainText("リンパ節検体")
win.chat.send_prompt("findings")
prompt = captured[-1][-1]["content"]
assert isinstance(prompt, str), "添付なしなら文字列のはず"
assert "リンパ節検体" in prompt and "補足指示" in prompt, "所見ボタンに補足が反映されません"
assert win.chat.input.toPlainText() == "", "送信後に入力欄がクリアされていません"
print(f"OK: チャット送信 (添付 {len(captured[0][-1]['content'])}部 / 補足反映)")

# --- 臨床情報 (任意項目) の保存・コンテキスト反映 ----------------------------
win.clinical_edits["site"].setText("頸部リンパ節")
win._save_case()
reloaded = win.store.load(win.case.id)
assert reloaded is not None and reloaded.clinical.get("site") == "頸部リンパ節", \
    "臨床情報の保存に失敗"
ctx = win._context()
assert "頸部リンパ節" in ctx["case_info"], "臨床情報がコンテキストに反映されません"
print("OK: 臨床情報の保存・プロンプト反映")

# --- 自由チャットへの臨床情報注入 (payloadのsystemメッセージ経由) -------------
captured2 = []
win.chat.set_context_getter(win._context)  # 148行目のスタブを本物に戻す
win.chat._start_worker = lambda msgs: captured2.append(msgs)  # worker起動をスタブ化
win.chat.chk_attach.setChecked(False)
win.chat.input.setPlainText("自由に質問")
win.chat._send_user_text()
sys0 = captured2[-1][0]
assert sys0["role"] == "system", "自由チャットの先頭はsystemメッセージのはず"
assert "臨床情報" in sys0["content"] and "頸部リンパ節" in sys0["content"], \
    "自由チャットに臨床情報が注入されていません"
assert "自由に質問" in captured2[-1][-1]["content"], "自由チャットの本文が変化しています"
print("OK: 自由チャットへの臨床情報・メモ注入")

# --- チャット履歴の症例JSON同期 ----------------------------------------------
win._save_case()
reloaded = win.store.load(win.case.id)
assert reloaded is not None, "症例の再読込に失敗"
assert len(reloaded.messages) == len(win.chat.messages) and reloaded.messages != [], \
    "チャット履歴が症例JSONに保存されていません"
print(f"OK: チャット履歴の保存 (メッセージ数={len(reloaded.messages)})")

# --- 履歴順序の固定 (クリック・保存で入れ替わらない) --------------------------
ids_before = [c["id"] for c in win.store.list_cases()]
win._save_case()  # クリック相当の保存 (updatedが更新される)
ids_after = [c["id"] for c in win.store.list_cases()]
assert ids_before == ids_after, "保存のたびに履歴の順序が入れ替わっています"
print("OK: 履歴順序の固定 (作成順・新しい順)")

# --- IME環境 (fcitx環境でのibusフォールバック) ------------------------------
from main import _setup_ime_env  # noqa: E402

os.environ["QT_IM_MODULE"] = "fcitx"
_setup_ime_env()
assert os.environ["QT_IM_MODULE"] == "ibus", \
    f"fcitx→ibusフォールバック失敗: {os.environ.get('QT_IM_MODULE')}"
print("OK: IME環境設定 (fcitx → ibus フォールバック)")

print("SMOKE_OK")
