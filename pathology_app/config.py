"""設定とデータディレクトリの管理。"""
from __future__ import annotations

import json
import os

APP_DIR = os.path.join(os.path.expanduser("~"), ".pathology_app")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
CASES_DIR = os.path.join(APP_DIR, "cases")
# 送信済み添付画像の保存先 (症例JSONにはここへのパスだけを記録する)
ATTACHMENTS_DIR = os.path.join(APP_DIR, "attachments")

DEFAULTS = {
    # 既定値はこの環境の unsloth ローカルエンドポイントに合わせる
    "base_url": "http://100.94.147.99:8888/v1",
    "model": "Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS",  # デフォルトは Qwen3.8 Flash
    "api_key": "",  # 空なら 環境変数 / ~/.dsh/.credentials.yaml から解決
    "api_key_env": "LOCAL_TALISCALE_API_KEY",
    "temperature": 0.4,
    "max_tokens": 2048,
}

# アプリ組み込みの追加モデルID。ハーネス登録一覧・エンドポイント一覧とは
# 独立に、常にモデル切替ドロップダウンへマージされる。
EXTRA_MODEL_IDS = [
    "unsloth/DeepSeek-V4-Flash-Vision-Exp-GGUF:UD-IQ2_XXS",  # ビジョン対応 (画像添付可)
]

# Swiftcam SDK (顕微鏡カメラ用)。環境変数 PATHOLOGY_SWIFTCAM_LIB で上書き可。
SWIFTCAM_LIB = "/usr/local/SwiftImaging3.0/libswiftcam.so"


def ensure_dirs() -> None:
    os.makedirs(CASES_DIR, exist_ok=True)
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                s.update({k: v for k, v in data.items() if k in DEFAULTS})
        except Exception:
            pass
    return s


def save_settings(s: dict) -> None:
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def harness_model_ids() -> list:
    """DeepSeek Harness プロファイル (~/.dsh/profiles/web/cordis.patch.yml) の
    llm-pi-ai providers.models に登録されたモデルIDを拾う簡易パース。

    アプリのモデル切替ドロップダウンに「ハーネスで登録済みのモデル」を
    自動登録するために使う。
    """
    path = os.path.join(
        os.path.expanduser("~"), ".dsh", "profiles", "web", "cordis.patch.yml"
    )
    ids: list = []
    if not os.path.exists(path):
        return ids
    try:
        in_models = False
        models_indent = -1
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if not in_models:
                    if stripped == "models:":
                        in_models = True
                        models_indent = len(line) - len(line.lstrip())
                    continue
                indent = len(line) - len(line.lstrip())
                if indent < models_indent:
                    break  # models: ブロックの外に出た
                if stripped.startswith("- id:"):
                    mid = stripped.split(":", 1)[1].strip().strip("'\"")
                    if mid and mid not in ids:
                        ids.append(mid)
    except Exception:
        pass
    return ids
