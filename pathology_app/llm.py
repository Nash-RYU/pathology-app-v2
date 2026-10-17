"""OpenAI互換(chat/completions)エンドポイントへのストリーミングLLMクライアント。"""
from __future__ import annotations

import json
import os

import httpx


class LLMError(RuntimeError):
    """LLM呼び出しの失敗を表す。"""


def _read_dsh_credentials_ref(env_name: str) -> str:
    """DeepSeek Harness (~/.dsh/.credentials.yaml) の refs からキーを拾う簡易パース。

    Harnessが保持している apiKeyEnv (例: LOCAL_TALISCALE_API_KEY) を
    アプリからも透過的に利用できるようにするための補助。
    """
    if not env_name:
        return ""
    path = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    if not os.path.exists(path):
        return ""
    try:
        in_refs = False
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped == "refs:":
                    in_refs = True
                    continue
                if in_refs:
                    # refsセクションの終わり判定: トップレベルキー(インデントなし)に戻ったら終了
                    if line and not line[0].isspace():
                        in_refs = False
                        continue
                    if stripped.startswith(env_name + ":"):
                        return stripped.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def resolve_api_key(settings: dict) -> str:
    """APIキーを解決する。優先順: 設定値 > 環境変数 > DSH credentials。"""
    key = (settings.get("api_key") or "").strip()
    if key:
        return key
    env_name = settings.get("api_key_env") or ""
    env_val = os.environ.get(env_name, "").strip() if env_name else ""
    if env_val:
        return env_val
    return _read_dsh_credentials_ref(env_name)


class LLMClient:
    def __init__(self, settings: dict):
        self.base_url = (settings.get("base_url") or "").rstrip("/")
        self.model = settings.get("model") or ""
        self.temperature = float(settings.get("temperature", 0.4))
        self.max_tokens = int(settings.get("max_tokens", 2048))
        self.api_key = resolve_api_key(settings)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def list_models(self) -> list[str]:
        """接続テスト用: モデルID一覧を返す。"""
        try:
            resp = httpx.get(
                self.base_url + "/models",
                headers=self._headers(),
                timeout=httpx.Timeout(connect=8, read=15, write=15, pool=8),
            )
        except httpx.HTTPError as e:
            raise LLMError(f"接続に失敗しました: {e}") from e
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json().get("data", [])
        return [m.get("id", "") for m in data if isinstance(m, dict)]

    # ----------------------------------------------------------- model id解決
    def _model_variants(self) -> list[str]:
        """設定モデルIDの表記ゆれ候補 (優先順) を返す。

        ハーネス(pi-ai)は `モデル名:量子化` や `unsloth/` 接頭辞の有無など
        独自のID表記を持つため、サーバが受け付けない場合は候補を順に試す。
        """
        base = (self.model or "").strip()
        if not base:
            return [base]
        cands = [base]
        if "/" not in base:
            cands.append("unsloth/" + base)
        bare = base.rsplit("/", 1)[-1] if "/" in base else base
        name = bare.split(":")[0] or bare
        # 「接頭辞なし+量子化サフィックスあり」(bare) も候補に含める。
        # この環境のエンドポイントは `モデル名:量子化` (unsloth/ なし) を
        # 列挙するため、`unsloth/X:Q` が通らないときの本命になる。
        for c in (bare, name, "unsloth/" + name):
            if c not in cands:
                cands.append(c)
        return cands

    @staticmethod
    def _is_model_error(e: LLMError) -> bool:
        """モデルIDが原因と思えるエラーか (バリアントリトライの判定用)。"""
        msg = str(e).lower()
        keys = ("model_not_found", "does not exist", "unknown model",
                "invalid model", "no model", "unsupported model")
        if any(k in msg for k in keys):
            return True
        return ("http 400" in msg or "http 404" in msg) and "model" in msg

    def stream_chat(self, messages: list[dict]):
        """メッセージ履歴を送り、本文差分をジェネレータで返す。

        モデルIDが原因のエラーのときは表記ゆれ候補に自動リトライする。
        失敗時は LLMError を送出する。
        """
        if not self.base_url:
            raise LLMError("エンドポイントURLが設定されていません。設定画面で入力してください。")
        if not self.model:
            raise LLMError("モデル名が設定されていません。設定画面で入力してください。")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        variants = self._model_variants()
        last_error: LLMError | None = None
        for i, model_id in enumerate(variants):
            payload["model"] = model_id
            try:
                yield from self._stream_request(dict(payload))
                return
            except LLMError as e:
                last_error = e
                if i + 1 < len(variants) and self._is_model_error(e):
                    continue  # 別の表記ゆれ候補でリトライ
                raise
        raise last_error  # pragma: no cover (上のraiseで到達しない)

    def _stream_request(self, payload: dict):
        """1リクエストのSSEストリーミング。"""
        timeout = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST", self.base_url + "/chat/completions",
                    json=payload, headers=self._headers(),
                ) as resp:
                    if resp.status_code != 200:
                        body = resp.read().decode("utf-8", "replace")
                        raise LLMError(f"HTTP {resp.status_code}: {body[:500]}")
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("error"):
                            msg = json.dumps(obj["error"], ensure_ascii=False)
                            raise LLMError(msg[:500])
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except httpx.HTTPError as e:
            raise LLMError(f"通信エラー: {e}") from e
