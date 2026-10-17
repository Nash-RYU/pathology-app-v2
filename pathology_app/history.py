"""症例履歴パネル。"""
from __future__ import annotations

import os

from PySide6.QtCore import QUrl, QSize, Qt, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QInputDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class HistoryPanel(QWidget):
    open_case = Signal(str)      # case_id
    new_case = Signal()
    delete_case = Signal(str)    # case_id
    rename_case = Signal(str, str)  # case_id, 新タイトル

    def __init__(self, parent=None):
        super().__init__(parent)
        self.case_id_by_row: dict[int, str] = {}
        self.case_title_by_row: dict[int, str] = {}
        self.att_paths: list[str] = []

        title = QLabel("履歴")
        title.setStyleSheet("font-weight: bold; padding: 2px;")
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._emit_open)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_context_menu)

        # --- 保存済み添付 (現在の症例で送信済みの画像) ------------------
        self.att_title = QLabel("保存済み添付 (この症例)")
        self.att_title.setStyleSheet("font-weight: bold; padding: 2px;")
        self.att_list = QListWidget()
        self.att_list.setIconSize(QSize(64, 64))
        self.att_list.setToolTip(
            "送信済み添付画像の一覧。ダブルクリックで既定のビューアで開きます。"
        )
        self.att_list.itemDoubleClicked.connect(self._open_attachment)

        btn_new = QPushButton("新規症例")
        btn_rename = QPushButton("名前変更")
        btn_del = QPushButton("削除")
        btn_new.clicked.connect(self.new_case.emit)
        btn_rename.clicked.connect(self._emit_rename)
        btn_del.clicked.connect(self._emit_delete)

        row = QHBoxLayout()
        row.addWidget(btn_new)
        row.addWidget(btn_rename)
        row.addWidget(btn_del)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(title)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.att_title)
        lay.addWidget(self.att_list, 1)
        lay.addLayout(row)

    def _row_of(self) -> int:
        return self.list.currentRow()

    def _emit_open(self, item: QListWidgetItem) -> None:
        cid = self.case_id_by_row.get(self.list.row(item))
        if cid:
            self.open_case.emit(cid)

    def _emit_rename(self) -> None:
        """選択中の症例の名前を変更する (ダイアログ入力)。"""
        row = self._row_of()
        if row < 0:
            return
        cid = self.case_id_by_row.get(row)
        if not cid:
            return
        current = self.case_title_by_row.get(row, "")
        new_title, ok = QInputDialog.getText(
            self, "名前の変更",
            "新しい症例タイトル:",
            text=current if current != "(無題)" else "",
        )
        if not ok:
            return
        new_title = new_title.strip()
        if new_title and new_title != current:
            self.rename_case.emit(cid, new_title)

    def _emit_delete(self) -> None:
        row = self._row_of()
        if row < 0:
            return
        cid = self.case_id_by_row.get(row)
        if not cid:
            return
        title = self.list.item(row).text()
        if QMessageBox.question(
            self, "削除の確認",
            f"症例「{title}」を削除しますか？\n(画像ファイル自体は削除されません)",
        ) == QMessageBox.StandardButton.Yes:
            self.delete_case.emit(cid)

    def _show_context_menu(self, pos) -> None:
        """右クリックメニュー (名前の変更 / 削除)。"""
        item = self.list.itemAt(pos)
        if item is None:
            return
        self.list.setCurrentItem(item)
        menu = QMenu(self)
        act_rename = menu.addAction("名前の変更")
        act_delete = menu.addAction("削除")
        act = menu.exec(self.list.viewport().mapToGlobal(pos))
        if act == act_rename:
            self._emit_rename()
        elif act == act_delete:
            self._emit_delete()

    def refresh(self, cases: list[dict]) -> None:
        current_id = None
        row = self.list.currentRow()
        if row >= 0:
            current_id = self.case_id_by_row.get(row)

        self.list.clear()
        self.case_id_by_row.clear()
        self.case_title_by_row.clear()
        for i, c in enumerate(cases):
            label = c.get("title", "(無題)")
            updated = c.get("updated", "")
            img = os.path.basename(c["image_path"]) if c.get("image_path") else ""
            text = f"{label}" + (f"  —  {updated}" if updated else "") \
                   + (f"  [{img}]" if img else "")
            self.list.addItem(text)
            self.case_id_by_row[i] = c["id"]
            self.case_title_by_row[i] = label

        # 再構築後も選択位置 (スクロール位置) を維持する
        if current_id is not None:
            for i, cid in self.case_id_by_row.items():
                if cid == current_id:
                    self.list.setCurrentRow(i)
                    break

    def show_attachments(self, items: list[dict]) -> None:
        """保存済み添付 (送信済み画像) の一覧をサムネイル付きで表示する。

        items: [{"path": str, "label": str}]。送信順 (メッセージ順)。
        パスだけを保持する運用のため、ここから直接ファイルを開ける。
        """
        self.att_list.clear()
        self.att_paths = []
        for it in items:
            path = it.get("path", "")
            self.att_paths.append(path)
            label = (it.get("label") or "").strip() or os.path.basename(path)
            icon = QIcon()
            pm = QPixmap(path)
            if not pm.isNull():
                icon = QIcon(pm)
            item = QListWidgetItem(icon, label)
            if not os.path.exists(path):
                item.setText(label + "  (ファイルなし)")
            item.setToolTip(path)
            self.att_list.addItem(item)

    def _open_attachment(self, item: QListWidgetItem) -> None:
        """保存済み添付を既定のビューアで開く。"""
        row = self.att_list.row(item)
        path = self.att_paths[row] if 0 <= row < len(self.att_paths) else ""
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
