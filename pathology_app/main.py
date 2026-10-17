"""メインウィンドウ組み込みとアプリ起動。"""
from __future__ import annotations

import os
import re
import sys

# 重要: PySide6を先にロードすると、環境によってはOpenSlideの共有ライブラリ解決が
# 失敗する (PySide6がバンドルするライブラリと衝突するため)。
# 必ずOpenSlideを先にロードしてからPySide6をimportすること。
try:
    import openslide  # noqa: F401  (早期ロードが目的)
except Exception:
    openslide = None  # type: ignore[assignment]

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

import config
from case import CLINICAL_FIELDS, Case, CaseStore
from chat import ChatPanel
from history import HistoryPanel
from llm import LLMClient
from modelbar import ModelBar, ModelListWorker
from settings import SettingsDialog
from viewer import WSI_EXTS, SlideViewer

try:
    from camera_panel import CameraPanel
except Exception:  # SDK未導入環境でも本体は起動できるようにする
    CameraPanel = None  # type: ignore[assignment]

IMAGE_FILTER = "画像・WSI (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.svs *.ndpi *.mrxs *.bif *.vsi *.scn);;すべてのファイル (*)"


class TitleWorker(QThread):
    """最初のやり取りの内容からLLMに短い症例タイトルを生成させる。"""
    got = Signal(str)
    failed = Signal(str)

    def __init__(self, client, user_text: str, reply_text: str, parent=None):
        super().__init__(parent)
        self.client = client
        self.user_text = user_text or "(内容なし)"
        self.reply_text = reply_text or ""
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        prompt = (
            "次の病理症例での最初のやり取りから、症例一覧に表示する短いタイトルを"
            "1つだけ生成してください。\n"
            "- 20文字以内・句読点なし・改行なし\n"
            "- 引用符・説明・前置き・「タイトル:」などのラベルは一切付けない\n"
            "- 標本の部位や疑われる病変が分かる名前にする "
            "(例: 「頸部リンパ節」「胃粘膜の慢性炎」)\n"
            "- 病変を特定できないときは部位や検体の種類に近い一般名\n\n"
            f"# 最初のユーザー発言\n{(self.user_text or '(なし)')[:1200]}\n\n"
            f"# 最初のAI回答 (前半)\n{self.reply_text[:800]}\n"
        )
        try:
            out = []
            for piece in self.client.stream_chat([
                {
                    "role": "system",
                    "content": (
                        "あなたは病理症例のタイトルを生成するアシスタントです。"
                        "出力はタイトル文字列のみ (それ以外は何も出力しない)。"
                    ),
                },
                {"role": "user", "content": prompt},
            ]):
                if self._stopped:
                    break
                out.append(piece)
            self.got.emit("".join(out))
        except Exception as e:
            self.failed.emit(str(e))


def _truncate_title(text: str, limit: int = 30) -> str:
    """LLM出力を短いタイトルに整形する (引用符・ラベル・改行を除去)。"""
    t = (text or "").strip()
    if not t:
        return ""
    t = t.splitlines()[0]  # 最初の行のみ
    # 「タイトル: xxx」などのラベルだけを除去 (本文中の「:」は保持)
    t = re.sub(r"^(タイトル|題名|title)\s*[:：]\s*", "", t.strip(), flags=re.I)
    return t.strip().strip("「」『』\"' ")[:limit]


def _first_reply_text(messages: list) -> str:
    """最初のAI回答の本文テキストを返す (自動タイトルの材料)。"""
    for m in messages or []:
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, str):
            t = c.strip()
        elif isinstance(c, list):
            t = "\n".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        else:
            t = ""
        if t:
            return t
    return ""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("病理画像AI診断支援")
        self.setAcceptDrops(True)

        self.store = CaseStore()
        self.case: Case = CaseStore.new_case()
        self.settings = config.load_settings()
        self.client = LLMClient(self.settings)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(1200)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._autosave)
        self._title_worker: TitleWorker | None = None
        self._title_worker_case_id = ""
        self._title_is_auto = False  # 自動生成タイトルか (手動リネームは False)

        # --- widgets ---------------------------------------------------
        self.viewer = SlideViewer()
        self.viewer.imageLoaded.connect(self._on_image_loaded)

        self.chat = ChatPanel()
        self.chat.set_llm_client(self.client)
        self.chat.set_case_id(self.case.id)
        self.chat.set_context_getter(self._context)
        self.chat.set_snapshot_getter(self._snapshot)
        self.chat.messages_changed.connect(self._on_messages_changed)

        # --- 臨床情報 (任意) ---------------------------------------------
        clinical_box = QGroupBox("臨床情報 (任意)")
        cgrid = QGridLayout()
        cgrid.setContentsMargins(8, 4, 8, 6)
        self.clinical_edits: dict = {}
        for row, (key, label) in enumerate(CLINICAL_FIELDS):
            lab = QLabel(label + ":")
            edit = QLineEdit()
            edit.setPlaceholderText("(未入力)")
            edit.textChanged.connect(self._schedule_autosave)
            cgrid.addWidget(lab, row, 0)
            cgrid.addWidget(edit, row, 1)
            self.clinical_edits[key] = edit
        clinical_box.setLayout(cgrid)

        notes_lay = QVBoxLayout()
        notes_lay.setContentsMargins(6, 2, 6, 6)
        notes_title = QLabel("自由メモ (自動保存)")
        notes_title.setStyleSheet("font-weight: bold; padding: 2px;")
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("臨床情報やメモを自由に記載できます。症例と一緒に保存されます。")
        self.notes.textChanged.connect(self._schedule_autosave)
        notes_lay.addWidget(clinical_box)
        notes_lay.addWidget(notes_title)
        notes_lay.addWidget(self.notes)
        notes_widget = QWidget()
        notes_widget.setLayout(notes_lay)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.chat)
        right.addWidget(notes_widget)
        right.setSizes([620, 280])

        central = QSplitter(Qt.Orientation.Horizontal)
        central.addWidget(self.viewer)
        central.addWidget(right)
        # ビューアを広めに (右列はチャットパネルの最小幅に合わせて細く)
        central.setSizes([1060, 440])
        self.setCentralWidget(central)

        self.history = HistoryPanel()
        self.history.open_case.connect(self._open_case)
        self.history.new_case.connect(self._new_case)
        self.history.delete_case.connect(self._delete_case)
        self.history.rename_case.connect(self._rename_case)
        dock = QDockWidget("履歴", self)
        dock.setWidget(self.history)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        # --- カメラパネル (Swiftcam顕微鏡カメラ) --------------------------
        # ドックを表示している間だけライブプレビューを動かし、閉じたら
        # カメラの占有を解放する (Swift Imaging 3.0 との二重起動を防ぐ)。
        self.camera_dock = None
        self.camera_panel = None
        if CameraPanel is not None:
            self.camera_panel = CameraPanel()
            self.camera_panel.snapshot_ready.connect(self.chat.add_camera_snapshot)
            self.camera_dock = QDockWidget("🎥 カメラ", self)
            self.camera_dock.setWidget(self.camera_panel)
            self.camera_dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetClosable
            )
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.camera_dock)
            self.camera_dock.visibilityChanged.connect(self._on_camera_visible)

        self.setStatusBar(QStatusBar())

        # --- actions ---------------------------------------------------
        act_open = QAction("開く…", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self.open_image_dialog)
        self.addAction(act_open)
        act_save = QAction("保存", self)
        act_save.setShortcut(QKeySequence.StandardKey.Save)
        act_save.triggered.connect(lambda: self._save_case(notify=True))
        self.addAction(act_save)
        act_new = QAction("新規症例", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._new_case)
        self.addAction(act_new)
        act_settings = QAction("設定…", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self._open_settings)
        self.addAction(act_settings)
        act_quit = QAction("終了", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        self.addAction(act_quit)

        toolbar = self.addToolBar("main")
        toolbar.setMovable(False)
        toolbar.addAction(act_open)
        toolbar.addAction(act_save)
        toolbar.addAction(act_new)
        toolbar.addAction(act_settings)

        # --- モデル切替バー --------------------------------------------
        self.model_bar = ModelBar()
        self.model_bar.model_changed.connect(self._on_model_changed)
        self.model_bar.refresh_requested.connect(self._refresh_models)
        self.model_bar.set_selected(self.settings.get("model", ""))
        toolbar.addWidget(self.model_bar)
        self._model_worker: ModelListWorker | None = None

        act_history = dock.toggleViewAction()
        act_history.setText("履歴パネル")
        menu = self.menuBar().addMenu("表示")
        menu.addAction(act_history)
        if self.camera_dock is not None:
            act_camera = self.camera_dock.toggleViewAction()
            act_camera.setText("カメラパネル")
            menu.addAction(act_camera)

        self._refresh_history()
        self._refresh_models()
        self._update_status("準備完了")

    # ------------------------------------------------------------- helpers
    def _collect_clinical(self) -> dict:
        return {
            k: e.text().strip()
            for k, e in self.clinical_edits.items()
            if e.text().strip()
        }

    def _set_clinical_widgets(self, clinical: dict) -> None:
        for k, e in self.clinical_edits.items():
            e.blockSignals(True)
            e.setText((clinical or {}).get(k, ""))
            e.blockSignals(False)

    def _context(self) -> dict:
        """チャット用のコンテキスト (標本情報/臨床情報/メモ/現在ビューPNG)。"""
        doc = self.viewer.doc
        if doc is None:
            case_info = "(画像未読込)"
            png = None
        else:
            mag = f" 対物{doc.magnification}倍" if doc.magnification != "?" else ""
            case_info = (
                f"- ファイル名: {os.path.basename(doc.path)}\n"
                f"- 形式: {doc.format_name}\n"
                f"- 解像度: {doc.width}×{doc.height}px{mag}\n"
                f"- 形式種別: {'WSI(ピラミッド)' if doc.is_wsi else '通常画像'}"
            )
            png = self.viewer.export_current_view()
        clinical = self._collect_clinical()
        clin_lines = []
        for key, label in CLINICAL_FIELDS:
            v = clinical.get(key, "")
            if v:
                clin_lines.append(f"- {label}: {v}")
        if clin_lines:
            case_info += "\n\n# 臨床情報\n" + "\n".join(clin_lines)
        return {
            "case_info": case_info,
            "notes": self.notes.toPlainText(),
            "png": png,
        }

    def _snapshot(self) -> dict:
        """チャット添付用に、現在ビューのスナップショットを取得する。

        戻り値: {"png": bytes, "note": str}。画像未読込のときは {}。
        note にはおおよその実効倍率 (対物倍率×ズーム。不明ならズーム値) と、
        画像全体の中での中心位置 (パーセント) を入れる。
        """
        doc = self.viewer.doc
        if doc is None or self.viewer.load_error:
            return {}
        png = self.viewer.export_current_view()
        if png is None:
            return {}
        z = self.viewer.zoom
        mag = (doc.magnification or "").rstrip("xX×")
        if mag and mag != "?":
            try:
                eff = float(mag) * z
                zoom_note = f"約{eff:g}×"
            except ValueError:
                zoom_note = f"ズーム{z:.2f}"
        else:
            zoom_note = f"ズーム{z:.2f}"
        if doc.width > 0 and doc.height > 0:
            cx = min(max(self.viewer.center.x() / doc.width, 0.0), 1.0)
            cy = min(max(self.viewer.center.y() / doc.height, 0.0), 1.0)
            pos_note = f"中心({cx:.0%},{cy:.0%})"
        else:
            pos_note = ""
        note = f"{zoom_note} {pos_note}".strip()
        return {"png": png, "note": note}

    def _update_status(self, text: str) -> None:
        model = self.settings.get("model", "?")
        base = self.settings.get("base_url", "?")
        self.statusBar().showMessage(f"{text}  |  モデル: {model}  |  {base}")

    def _on_camera_visible(self, visible: bool) -> None:
        """カメラドックの開閉に合わせてライブプレビューを開始・停止する。"""
        if self.camera_panel is None:
            return
        if visible:
            self.camera_panel.start_camera()
        else:
            self.camera_panel.stop_camera()

    def _refresh_history(self) -> None:
        self.history.refresh(self.store.list_cases())

    def _case_attachments(self) -> list[dict]:
        """現在の症例 (チャット履歴) の保存済み添付を送信順に返す。

        [{"path": str, "label": str}]。パス参照だけをJSONに保存する運用なので、
        履歴パネルにここへ格納されたPNGを表示・直接開けるようにする。
        """
        items: list[dict] = []
        seen: set[str] = set()
        for m in self.chat.messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for p in content:
                if not (isinstance(p, dict) and p.get("type") == "image_url"):
                    continue
                iu = p.get("image_url") or {}
                path = iu.get("path") or ""
                if path and path not in seen:
                    seen.add(path)
                    items.append({"path": path, "label": iu.get("label", "")})
        return items

    def _refresh_saved_attachments(self) -> None:
        """履歴パネルの「保存済み添付」一覧を現在の症例で更新する。"""
        self.history.show_attachments(self._case_attachments())

    # ------------------------------------------------------------- model選択
    def _refresh_models(self) -> None:
        """ハーネス登録済みID+組み込み追加IDを先に投入し、エンドポイント一覧を取得してマージ。"""
        ids = list(config.harness_model_ids()) + list(config.EXTRA_MODEL_IDS)
        if self.settings.get("model"):
            ids.insert(0, self.settings["model"])
        self.model_bar.set_models(ids)
        if self._model_worker is not None and self._model_worker.isRunning():
            return
        worker = ModelListWorker(self.client)
        worker.fetched.connect(self._on_models_fetched)
        worker.failed.connect(self._on_models_failed)
        self._model_worker = worker
        worker.start()

    def _on_models_fetched(self, models: list) -> None:
        ids = (
            list(config.harness_model_ids())
            + list(config.EXTRA_MODEL_IDS)
            + list(models)
        )
        self.model_bar.set_models(ids)
        self.model_bar.set_selected(self.settings.get("model", ""))
        self._update_status("モデル一覧を更新しました")

    def _on_models_failed(self, message: str) -> None:
        self._update_status("モデル一覧の取得に失敗 — ハーネス登録済みIDのみ表示しています")

    def _on_model_changed(self, model_id: str) -> None:
        self.settings["model"] = model_id
        config.save_settings(self.settings)
        self.client = LLMClient(self.settings)
        self.chat.set_llm_client(self.client)
        self._update_status("モデルを切り替えました")

    def _schedule_autosave(self) -> None:
        self._autosave_timer.start()

    def _on_messages_changed(self) -> None:
        """チャット履歴が変わったら自動保存と自動タイトル生成を準備する。"""
        self._schedule_autosave()
        self._maybe_generate_title()

    # ------------------------------------------------- タイトルの自動生成
    def _maybe_generate_title(self) -> None:
        """症例タイトルが未設定なら、最初のやり取りからLLMに生成させる。

        タイトルが「(無題)」・空・自動生成のもののみ対象。手動で付けた
        タイトルは上書きしない。既にLLMタイトルが確定した症例では再生成しない。
        """
        t = self.case.title.strip()
        if t and t != "(無題)" and not self._title_is_auto:
            return
        if self._title_worker is not None and self._title_worker.isRunning():
            return
        reply = _first_reply_text(self.chat.messages)
        if not reply:
            return  # AIの回答がない (送信失敗のまま) なら生成しない
        user_text = Case._first_user_text(self.chat.messages)
        worker = TitleWorker(self.client, user_text, reply)
        worker.got.connect(self._on_title_generated)
        worker.failed.connect(self._on_title_failed)
        self._title_worker = worker
        self._title_worker_case_id = self.case.id
        worker.start()

    def _on_title_generated(self, text: str) -> None:
        title = _truncate_title(text)
        if not title or title == "(無題)":
            return
        if self._title_worker_case_id != self.case.id:
            return  # 生成中に別の症例へ切り替えられた
        t = self.case.title.strip()
        if t and t != "(無題)" and not self._title_is_auto:
            return  # 手動タイトルは上書きしない
        self.case.title = title
        self._title_is_auto = False  # LLMタイトルが確定 (再生成しない)
        self.case.save()
        self._refresh_history()
        self.setWindowTitle(f"病理画像AI診断支援 — {self.case.title}")
        self._update_status("症例タイトルを自動生成しました")

    def _on_title_failed(self, message: str) -> None:
        # タイトル生成の失敗はチャット操作を妨げない (ステータス表示のみ)
        self._update_status(f"タイトル自動生成に失敗 ({message[:60]}…)")

    def _rename_case(self, case_id: str, new_title: str) -> None:
        """履歴パネルからの名前変更を反映する。"""
        new_title = (new_title or "").strip()
        if not case_id or not new_title:
            return
        if self.case.id == case_id:
            self.case.title = new_title
            self._title_is_auto = False  # 手動タイトルを保護する
            self.case.save()
            self.setWindowTitle(f"病理画像AI診断支援 — {self.case.title}")
            self._update_status("症例名を変更しました")
        else:
            c = self.store.load(case_id)
            if c is None:
                QMessageBox.warning(self, "名前の変更", "症例を読み込めませんでした。")
                return
            c.title = new_title
            c.save()
            self._update_status("症例名を変更しました")
        self._refresh_history()

    def _autosave(self) -> None:
        self._save_case(notify=False)

    def _save_case(self, notify: bool = False) -> None:
        self.case.notes = self.notes.toPlainText()
        self.case.clinical = self._collect_clinical()
        # チャット履歴 (自由チャット・所見・鑑別の全やり取り) を症例に同期して保存
        self.case.messages = [dict(m) for m in self.chat.messages]
        before = self.case.title.strip()
        self.case.save()  # ここで auto_title がローカル即時タイトルを付ける
        if before in ("", "(無題)") and self.case.title.strip() \
                and self.case.title.strip() != before:
            self._title_is_auto = True  # ローカル自動タイトル (LLMで上書き可)
        self._refresh_history()
        self._refresh_saved_attachments()
        self.setWindowTitle(f"病理画像AI診断支援 — {self.case.title}")
        if notify:
            self._update_status("保存しました")

    # ------------------------------------------------------------- actions
    def open_image_dialog(self) -> None:
        start = os.path.dirname(self.case.image_path) if self.case.image_path else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "画像・WSIを開く", start, IMAGE_FILTER)
        if path:
            self.viewer.load(path)

    def _on_image_loaded(self, path: str) -> None:
        self.case.image_path = path
        self._save_case(notify=False)
        if self.viewer.doc is not None and self.viewer.load_error == "":
            self._update_status("画像を読み込みました")
        else:
            self._update_status("画像の読み込みに失敗")

    def _new_case(self) -> None:
        self._save_case(notify=False)
        self.case = CaseStore.new_case()
        self.chat.set_case_id(self.case.id)
        self._title_is_auto = False
        self._title_worker_case_id = self.case.id
        self.notes.clear()
        self._set_clinical_widgets({})
        self.chat.load_messages([])
        self.viewer.close_image()
        self.setWindowTitle(f"病理画像AI診断支援 — {self.case.title}")
        self._refresh_history()
        self._refresh_saved_attachments()
        self._update_status("新規症例を作成しました")

    def _open_case(self, case_id: str) -> None:
        self._save_case(notify=False)
        loaded = self.store.load(case_id)
        if loaded is None:
            QMessageBox.warning(self, "履歴", "症例を読み込めませんでした。")
            return
        self.case = loaded
        self.chat.set_case_id(self.case.id)
        # 読み込んだタイトルは原則手動扱い (「(無題)」のときは自動生成を試みる)
        self._title_is_auto = False
        self.notes.setPlainText(self.case.notes)
        self._set_clinical_widgets(self.case.clinical)
        self.chat.load_messages(self.case.messages)
        if self.case.image_path and os.path.exists(self.case.image_path):
            self.viewer.load(self.case.image_path)
        else:
            self.viewer.close_image()
            if self.case.image_path:
                self._update_status("画像ファイルが見つかりません (メモ・チャットは読み込み済み)")
        self.setWindowTitle(f"病理画像AI診断支援 — {self.case.title}")
        self._refresh_history()
        self._refresh_saved_attachments()
        self._title_worker_case_id = self.case.id
        self._maybe_generate_title()  # 「(無題)」の旧症例も次の表示で直せる

    def _delete_case(self, case_id: str) -> None:
        self.store.delete(case_id)
        if self.case.id == case_id:
            self._new_case()
        else:
            self._refresh_history()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() and dlg.result() == 1:
            self.settings = dlg.result_settings()
            config.save_settings(self.settings)
            self.client = LLMClient(self.settings)
            self.chat.set_llm_client(self.client)
            self.model_bar.set_selected(self.settings.get("model", ""))
            self._update_status("設定を更新しました")

    # ------------------------------------------------------------- drag&drop
    def dragEnterEvent(self, ev) -> None:
        urls = ev.mimeData().urls()
        if urls and any(
            u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower() in WSI_EXTS | {
                ".png", ".jpg", ".jpeg", ".bmp"
            }
            for u in urls
        ):
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        for u in ev.mimeData().urls():
            if u.isLocalFile():
                self.viewer.load(u.toLocalFile())
                break

    def closeEvent(self, ev) -> None:
        self._save_case(notify=False)
        if self._title_worker is not None and self._title_worker.isRunning():
            self._title_worker.stop()
            self._title_worker.wait(1000)
        if self.camera_panel is not None:
            self.camera_panel.shutdown()
        super().closeEvent(ev)


def _setup_ime_env() -> None:
    """fcitx5環境での日本語入力_FIX。

    pip版PySide6にはfcitxの入力コンテキストプラグインが含まれないため、
    QT_IM_MODULE=fcitx のままだとQtがサイレントに「入力メソッドなし」に
    フォールバックし、日本語が入力できない。fcitx5はIBus互換インターフェース
    (org.freedesktop.IBus) をセッションD-Busで提供しているため、バンドル済みの
    ibusプラグイン経由に切り替えて日本語入力を可能にする。
    """
    if os.environ.get("QT_IM_MODULE", "") != "fcitx":
        return
    has_fcitx_plugin = False
    try:
        from PySide6.QtCore import QLibraryInfo
        loc = getattr(QLibraryInfo, "Plugins", None) or getattr(
            QLibraryInfo, "PluginsPath", None
        )
        plugins = QLibraryInfo.path(loc) if loc else ""
        d = os.path.join(plugins, "platforminputcontexts")
        has_fcitx_plugin = os.path.isdir(d) and any(
            f.startswith("libfcitx") for f in os.listdir(d)
        )
    except Exception:
        pass
    if not has_fcitx_plugin:
        os.environ["QT_IM_MODULE"] = "ibus"


def _ensure_desktop_file() -> None:
    """GNOMEドック向けの .desktop エントリを用意する。

    WM_CLASS に日本語を入れると、Latin-1 (STRING型) として解釈する
    ツールチップ実装でアプリ名が文字化けするため、アプリ名 (クラス名) は
    ASCIIの pathology_app にし、日本語表示名はこの .desktop ファイルから
    解決させる。あわせてドックのアイコンも設定される。
    """
    try:
        apps_dir = os.path.join(
            os.path.expanduser("~"), ".local", "share", "applications"
        )
        os.makedirs(apps_dir, exist_ok=True)
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon = os.path.join(app_dir, "screenshot.png")
        exec_path = os.path.join(app_dir, "run.sh")
        icon_line = f"Icon={icon}\n" if os.path.exists(icon) else ""
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=病理画像AI診断支援\n"
            "Name[ja]=病理画像AI診断支援\n"
            "Comment=病理画像を表示しながらローカルLLMに所見・鑑別診断を依頼\n"
            f"Exec={exec_path}\n"
            f"Path={app_dir}\n"
            "Terminal=false\n"
            + icon_line +
            "StartupWMClass=pathology_app\n"
            "Categories=Science;Medical;Education;\n"
        )
        path = os.path.join(apps_dir, "pathology-app.desktop")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        pass  # デスクトップ環境が無い・書き込めない場合は無視


def run() -> None:
    config.ensure_dirs()
    _setup_ime_env()
    _ensure_desktop_file()
    app = QApplication(sys.argv)
    # WM_CLASSはLatin-1 (STRING型) で解釈される環境があるためASCIIにする
    # (ドックのツールチップでのアプリ名文字化け対策)。
    # 日本語の表示名はウィンドウタイトルと .desktop から解決される。
    app.setApplicationName("pathology_app")
    try:
        app.setDesktopFileName("pathology-app")
    except Exception:
        pass
    icon_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "screenshot.png",
    )
    if os.path.exists(icon_path):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(icon_path))
    win = MainWindow()
    win.resize(1600, 1000)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
