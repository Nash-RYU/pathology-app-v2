"""設定ダイアログ。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from llm import LLMClient, LLMError


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LLM設定")
        self.setMinimumWidth(520)
        self._settings = dict(settings)

        self.ed_base = QLineEdit(settings.get("base_url", ""))
        self.ed_model = QLineEdit(settings.get("model", ""))
        self.ed_api_key = QLineEdit(settings.get("api_key", ""))
        self.ed_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_api_key.setPlaceholderText(
            "空欄なら 環境変数 / ~/.dsh/.credentials.yaml から自動解決"
        )
        self.ed_env = QLineEdit(settings.get("api_key_env", ""))
        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(0.0, 2.0)
        self.sp_temp.setSingleStep(0.1)
        self.sp_temp.setValue(float(settings.get("temperature", 0.4)))
        self.sp_max = QSpinBox()
        self.sp_max.setRange(64, 131072)
        self.sp_max.setValue(int(settings.get("max_tokens", 2048)))

        form = QFormLayout()
        form.addRow("エンドポイントURL", self.ed_base)
        form.addRow("モデルID", self.ed_model)
        form.addRow("APIキー (任意)", self.ed_api_key)
        form.addRow("APIキー環境変数名", self.ed_env)
        form.addRow("temperature", self.sp_temp)
        form.addRow("max_tokens", self.sp_max)

        btn_test = QPushButton("接続テスト")
        btn_test.clicked.connect(self._test_connection)
        form.addRow("", btn_test)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def _test_connection(self) -> None:
        settings = dict(self._settings)
        settings.update({
            "base_url": self.ed_base.text().strip(),
            "api_key": self.ed_api_key.text().strip(),
            "api_key_env": self.ed_env.text().strip(),
        })
        client = LLMClient(settings)
        try:
            models = client.list_models()
        except LLMError as e:
            QMessageBox.critical(self, "接続テスト", f"失敗:\n{e}")
            return
        names = "\n".join(models[:8]) or "(モデルなし)"
        QMessageBox.information(
            self, "接続テスト", f"接続成功。モデル一覧 (上位8件):\n{names}"
        )

    def result_settings(self) -> dict:
        self._settings.update({
            "base_url": self.ed_base.text().strip(),
            "model": self.ed_model.text().strip(),
            "api_key": self.ed_api_key.text().strip(),
            "api_key_env": self.ed_env.text().strip(),
            "temperature": self.sp_temp.value(),
            "max_tokens": self.sp_max.value(),
        })
        return self._settings
