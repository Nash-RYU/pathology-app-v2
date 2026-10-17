"""モデル切替バー: ドロップダウンでLLMモデルを選択する。

ソース:
- エンドポイント /v1/models の一覧 (バックグラウンドで取得)
- ハーネスプロファイルに登録済みのモデルID (config.harness_model_ids)
- アプリ組み込みの追加モデルID (config.EXTRA_MODEL_IDS)
- 設定済みモデルID
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from llm import LLMClient, LLMError


class ModelListWorker(QThread):
    """/v1/models の取得を別スレッドで行う。"""
    fetched = Signal(list)
    failed = Signal(str)

    def __init__(self, client: LLMClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        try:
            models = self.client.list_models()
        except LLMError as e:
            if not self._stopped:
                self.failed.emit(str(e))
            return
        if not self._stopped:
            self.fetched.emit(models)


class ModelBar(QWidget):
    """ツールバーに埋め込むモデル切替ウィジェット。"""
    model_changed = Signal(str)      # ユーザーが選択を変更した
    refresh_requested = Signal()     # 再読込ボタンが押された

    def __init__(self, parent=None):
        super().__init__(parent)
        label = QLabel("モデル:")
        label.setStyleSheet("font-weight: bold;")

        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo.setMinimumContentsLength(36)
        self.combo.setToolTip(
            "LLMモデルを選択します。\n"
            "ハーネスで登録済みのモデルと、エンドポイントの一覧をマージして表示します。"
        )
        self.combo.activated.connect(self._on_activated)

        self.btn_refresh = QPushButton("🔄")
        self.btn_refresh.setToolTip("エンドポイントからモデル一覧を再取得")
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.addWidget(label)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.btn_refresh)

    # ------------------------------------------------------------- internal
    def _on_activated(self, index: int) -> None:
        mid = self.combo.itemData(index)
        if mid:
            self.model_changed.emit(str(mid))

    # -------------------------------------------------------------- public
    def set_models(self, ids: list) -> None:
        """モデルIDリストを(マージ・重複除去して)設定する。選択は維持。"""
        current = self.selected()
        seen = set()
        merged = []
        for mid in ids:
            mid = (mid or "").strip()
            if mid and mid not in seen:
                seen.add(mid)
                merged.append(mid)
        self.combo.blockSignals(True)
        self.combo.clear()
        for mid in merged:
            self.combo.addItem(mid, mid)
        self.combo.blockSignals(False)
        if current:
            self.set_selected(current)

    def set_selected(self, model_id: str) -> None:
        """指定モデルを選択する (無ければ先頭に挿入)。シグナルは出さない。"""
        if not model_id:
            return
        idx = self.combo.findData(model_id)
        if idx < 0:
            self.combo.blockSignals(True)
            self.combo.insertItem(0, model_id, model_id)
            idx = 0
            self.combo.blockSignals(False)
        self.combo.setCurrentIndex(idx)

    def selected(self) -> str:
        data = self.combo.currentData()
        return str(data) if data else self.combo.currentText()
