"""チャットパネル: 所見 / 鑑別診断ボタン + 自由入力チャット (ストリーミング表示)。

複数画像添付に対応: 「＋ 画像…」ボタンでの複数選択、チャットパネルへの
ドラッグ&ドロップ、既存の「画像を添付」チェック (現在ビュー) とまとめて送信。
"""
from __future__ import annotations

import base64
import os
import re
import time

from PySide6.QtCore import QBuffer, QIODevice, QUrl, Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import prompts
from config import ATTACHMENTS_DIR, ensure_dirs  # type: ignore[import]

# LLMへ添付できる画像の拡張子。WSIは直接は送れないため、OpenSlideで
# 全体像 (低倍サムネイル) を生成して添付する。
WSI_EXTS = (".svs", ".ndpi", ".mrxs", ".bif", ".vsi", ".scn")
ATTACH_EXTS = (
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp",
) + WSI_EXTS
ATTACH_FILTER = (
    "画像・WSI (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif *.webp"
    " *.svs *.ndpi *.mrxs *.bif *.vsi *.scn);;すべてのファイル (*)"
)


def _wsi_overview_png(path: str, max_px: int = 1280) -> bytes | None:
    """WSIファイルの全体像 (低倍サムネイル) をPNGバイト列で返す。

    OpenSlideで最適なピラミッドレベルを読み、長辺が max_px 以下になるよう
    縮小する。OpenSlide未導入・未対応形式のときは None。
    """
    try:
        import openslide
        import io
        from PIL import Image
        slide = openslide.OpenSlide(path)
        w0, h0 = slide.dimensions
        ds = max(w0, h0) / max_px
        level = slide.get_best_level_for_downsample(ds) if ds > 1 else 0
        w, h = slide.level_dimensions[level]
        img = slide.read_region((0, 0), level, (w, h)).convert("RGB")
        m = max(img.width, img.height)
        if m > max_px:
            r = max_px / m
            img = img.resize(
                (max(1, round(img.width * r)), max(1, round(img.height * r))),
                Image.Resampling.LANCZOS if hasattr(Image, "Resampling")
                else Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return bytes(buf.getvalue())
    except Exception:
        return None


def _png_to_thumb(data: bytes, thumb_px: int = 72) -> QPixmap | None:
    """PNGバイト列からサムネイルQPixmapを作る。失敗時はNone。"""
    img = QImage()
    if not img.loadFromData(data, "PNG"):
        return None
    return QPixmap.fromImage(img.scaled(
        thumb_px, thumb_px,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ))


def load_png_and_thumb(
    path: str, max_px: int = 1280, thumb_px: int = 72
) -> tuple[bytes | None, QPixmap | None]:
    """画像ファイルを読み、(送信用PNGバイト列, サムネイルQPixmap) を返す。

    長辺が max_px を超える場合は縮小してからPNGへ再エンコードする
    (巨大画像の帯域・トークン対策)。WSI形式はOpenSlideで全体像を生成する。
    読み込みに失敗したら (None, None)。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in WSI_EXTS:
        data = _wsi_overview_png(path, max_px)
        if data is not None:
            return data, _png_to_thumb(data, thumb_px)
        return None, None
    img = QImage(path)
    if img.isNull() and ext in (".tif", ".tiff"):
        # ピラミッドtiffはQImageで読めないことがある → OpenSlideを試す
        data = _wsi_overview_png(path, max_px)
        if data is not None:
            return data, _png_to_thumb(data, thumb_px)
        return None, None
    if img.isNull():
        return None, None
    w, h = img.width(), img.height()
    m = max(w, h)
    if m > max_px:
        r = max_px / m
        img = img.scaled(
            max(1, round(w * r)), max(1, round(h * r)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        return None, None
    return bytes(buf.data()), _png_to_thumb(bytes(buf.data()), thumb_px)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _image_paths_in_urls(urls) -> list[str]:
    """mimeURLのうちローカル画像ファイルのパス一覧を返す。"""
    return [
        u.toLocalFile()
        for u in urls
        if u.isLocalFile() and u.toLocalFile().lower().endswith(ATTACH_EXTS)
    ]


def _attachment_slug(label: str) -> str:
    """添付ラベルからファイル名に使えるスラッグを作る (日本語はそのまま残す)。"""
    s = re.sub(r"[^\w\-]+", "_", (label or "").strip()).strip("_")
    return s[:24] or "img"


def _attachment_display_name(iu: dict) -> str:
    """image_urlパートの表示用ラベル (labelがなければファイル名)。"""
    label = (iu.get("label") or "").strip()
    if label:
        return label
    path = iu.get("path") or ""
    if path:
        return os.path.basename(path)
    return "(添付画像)"


def _data_url_png(url: str) -> bytes | None:
    """data:image/...;base64 URLからPNGバイト列を取り出す (旧形式の履歴用)。"""
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    try:
        return base64.b64decode(url.split("base64,", 1)[-1])
    except Exception:
        return None


class ChatWorker(QThread):
    """LLMストリーミングを別スレッドで実行し、UIへシグナルで通知する。"""
    chunk = Signal(str)
    finished_ok = Signal(str)   # 完全な本文
    failed = Signal(str)

    def __init__(self, client, messages: list[dict], parent=None):
        super().__init__(parent)
        self.client = client
        self.messages = messages
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        full = []
        try:
            for piece in self.client.stream_chat(self.messages):
                if self._stopped:
                    break
                full.append(piece)
                self.chunk.emit(piece)
        except Exception as e:  # LLMError / httpx 例外をUIに報告
            self.failed.emit(str(e))
            return
        self.finished_ok.emit("".join(full))


class _DropWidgetMixin:
    """画像ファイルのドロップを添付として受け取るための共通実装。

    使用側クラスは `images_dropped = Signal(list)` を自ら宣言する
    (SignalはQObject派生クラスで宣言する必要があるため)。
    画像以外のドロップ (テキスト貼り付けなど) は従来動作に委ねる。
    """

    def dragEnterEvent(self, ev):
        urls = ev.mimeData().urls()
        if urls and _image_paths_in_urls(urls):
            ev.acceptProposedAction()
            return
        super().dragEnterEvent(ev)

    def dropEvent(self, ev):
        paths = _image_paths_in_urls(ev.mimeData().urls())
        if paths:
            ev.acceptProposedAction()
            self.images_dropped.emit(paths)
            return
        super().dropEvent(ev)


class _InputEdit(_DropWidgetMixin, QPlainTextEdit):
    """Enterで送信、Shift+Enterで改行の入力欄。画像のドロップ添付も可能。"""
    return_pressed = Signal()
    images_dropped = Signal(list)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not (ev.modifiers() & Qt.ShiftModifier):
            self.return_pressed.emit()
            ev.accept()
            return
        super().keyPressEvent(ev)


class _DropBrowser(_DropWidgetMixin, QTextBrowser):
    """チャット表示欄。画像ファイルのドロップ添付も可能。"""
    images_dropped = Signal(list)


class ChatPanel(QWidget):
    messages_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = None
        self.context_getter = None  # () -> {"case_info": str, "notes": str, "png": bytes|None}
        self.snapshot_getter = None  # () -> {"png": bytes|None, "note": str} | {}
        self.messages: list[dict] = []
        self.worker: ChatWorker | None = None
        self._stream_accum: list[str] = []
        # 保留中の添付画像: {"path": str, "png": bytes, "thumb": QPixmap,
        #                   "label": str (スナップのとき)}
        self._pending_images: list[dict] = []
        self._snap_counter = 0
        self._case_id = ""       # 現在の症例ID (添付ファイル名に使う)
        self._attach_seq = 0     # 同一送信バッチ内の連番
        self._emb_seq = 0        # 履歴表示用の埋め込み画像リソース連番

        self.browser = _DropBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setReadOnly(True)
        self.browser.document().setDefaultStyleSheet(
            "p{margin:2px 0;}"
            ".meta{color:#8892a8;}"
            ".user{color:#ffd479;}"
            ".ai{color:#bfe3ff;}"
            ".err{color:#ff8a80;}"
        )
        self.browser.images_dropped.connect(self._add_images_from_paths)

        self.input = _InputEdit()
        self.input.setPlaceholderText(
            "質問を入力してEnter (Shift+Enterで改行)。所見・鑑別診断ボタンでも質問できます。"
        )
        self.input.setFixedHeight(72)
        self.input.return_pressed.connect(self._send_user_text)
        self.input.images_dropped.connect(self._add_images_from_paths)

        self.btn_send = QPushButton("送信")
        self.btn_findings = QPushButton("🩠 所見")
        self.btn_diff = QPushButton("🧬 鑑別診断")
        self.btn_clear = QPushButton("クリア")
        self.chk_attach = QCheckBox("画像を添付")
        self.chk_attach.setToolTip(
            "現在ビューアに表示中の画像をLLMへ送ります。"
            "ビジョン非対応モデルではエラーになることがあります。"
        )
        self.btn_add_img = QPushButton("＋ 画像…")
        self.btn_add_img.setToolTip(
            "画像ファイルを複数枚選んで添付します (Ctrl/Shiftで複数選択)。"
            "WSIファイルはOpenSlideで全体像に縮小して添付します。"
            "チャットパネルへのドラッグ&ドロップでも追加できます。"
            "サムネイルの ✕ で個別に解除できます。"
        )
        self.btn_add_img.clicked.connect(self._pick_images)
        self.btn_snap = QPushButton("📷 スナップ")
        self.btn_snap.setToolTip(
            "現在ビューアに表示中の範囲をそのまま添付に追加します。"
            "パン・ズームで場所や倍率を変えながら複数枚撮影できます"
            " (低倍の全体像と高倍の部分像などを)。ラベルにおおよその実効倍率と"
            "中心位置が付きます。"
        )
        self.btn_snap.clicked.connect(self._add_snapshot)
        self.btn_send.clicked.connect(self._on_send_clicked)
        self.btn_findings.clicked.connect(lambda: self.send_prompt("findings"))
        self.btn_diff.clicked.connect(lambda: self.send_prompt("differential"))
        self.btn_clear.clicked.connect(self.clear_chat)

        # --- 添付サムネイル帯 (画像を追加したときだけ表示) ---------------
        self.attach_strip = QScrollArea()
        self.attach_strip.setWidgetResizable(True)
        self.attach_strip.setFrameShape(QFrame.Shape.NoFrame)
        self.attach_strip.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.attach_strip.setFixedHeight(88)
        self.attach_row = QWidget(self.attach_strip)
        self.attach_lay = QHBoxLayout(self.attach_row)
        self.attach_lay.setContentsMargins(2, 2, 2, 2)
        self.attach_lay.setSpacing(6)
        self.attach_strip.setWidget(self.attach_row)
        self.attach_strip.setVisible(False)

        self.setAcceptDrops(True)

        # プロンプト系ボタンと送信系を1行目、添付系を2行目に分けて
        # パネルの最小幅を狭くし、ビューア側の表示領域を確保する
        row = QHBoxLayout()
        row.addWidget(self.btn_findings)
        row.addWidget(self.btn_diff)
        row.addStretch(1)
        row.addWidget(self.btn_send)
        row.addWidget(self.btn_clear)

        row_att = QHBoxLayout()
        row_att.addWidget(self.btn_snap)
        row_att.addWidget(self.btn_add_img)
        row_att.addWidget(self.chk_attach)
        row_att.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        lay.addWidget(self.browser, 1)
        lay.addWidget(self.input)
        lay.addWidget(self.attach_strip)
        lay.addLayout(row)
        lay.addLayout(row_att)

        self._set_busy(False)

    # -------------------------------------------------------------- wiring
    def set_llm_client(self, client) -> None:
        self.client = client

    def set_case_id(self, case_id: str) -> None:
        """現在の症例IDを設定する (添付PNGのファイル名に使う)。"""
        self._case_id = case_id or ""

    def set_context_getter(self, fn) -> None:
        self.context_getter = fn

    def set_snapshot_getter(self, fn) -> None:
        """スナップ取得関数を設定する () -> {"png": bytes, "note": str} | {}。"""
        self.snapshot_getter = fn

    def _set_busy(self, busy: bool) -> None:
        self.btn_findings.setEnabled(not busy)
        self.btn_diff.setEnabled(not busy)
        self.btn_clear.setEnabled(not busy)
        self.btn_send.setText("停止" if busy else "送信")

    # ----------------------------------------------------------- 添付画像
    def _pick_images(self) -> None:
        """ファイルダイアログで画像を複数選択して添付に追加する。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添付する画像を選ぶ (複数選択可)",
            os.path.expanduser("~"), ATTACH_FILTER,
        )
        if paths:
            self._add_images_from_paths(paths)

    def _item_label(self, item: dict) -> str:
        """添付アイテムの表示・送信用ラベルを返す。"""
        return item.get("label") or os.path.basename(item["path"])

    def _add_images_from_paths(self, paths) -> None:
        """画像ファイルを保留中の添付に追加する (重複は無視、失敗は通知)。"""
        added = 0
        for p in paths:
            if any(item["path"] == p for item in self._pending_images):
                continue
            png, thumb = load_png_and_thumb(p)
            if png is None:
                hint = (
                    " (WSIはOpenSlide環境でのみ添付できます)"
                    if p.lower().endswith(WSI_EXTS + (".tif", ".tiff")) else ""
                )
                self._append_error(
                    f"画像を読み込めず添付をスキップ: {os.path.basename(p)}{hint}"
                )
                continue
            self._pending_images.append({"path": p, "png": png, "thumb": thumb})
            added += 1
        if added:
            self._rebuild_attach_strip()

    def _add_snapshot(self) -> None:
        """現在ビューのスナップショットを添付に追加する。

        パン・ズームを変えながら複数回呼ぶことで、低倍の全体像と高倍の
        部分像などを撮り溜めてからまとめて送信できる。
        """
        if self.snapshot_getter is None:
            self._append_error("スナップ取得先 (ビューア) が未接続です。")
            return
        try:
            ret = self.snapshot_getter() or {}
        except Exception as e:
            self._append_error(f"スナップの取得に失敗: {e}")
            return
        png = ret.get("png")
        if not png:
            self._append_error("画像が読み込まれていないためスナップを追加できません。")
            return
        self._snap_counter += 1
        note = (ret.get("note") or "").strip()
        label = f"スナップ{self._snap_counter} ({note})" if note else f"スナップ{self._snap_counter}"
        thumb = _png_to_thumb(png, 72)
        if thumb is None:
            self._append_error("スナップ画像のエンコードに失敗しました。")
            self._snap_counter -= 1
            return
        self._pending_images.append(
            {"path": "", "png": png, "thumb": thumb, "label": label}
        )
        self._rebuild_attach_strip()

    def add_camera_snapshot(self, png: bytes, note: str = "") -> None:
        """カメラパネル (PreviewWorker) からのスナップを添付に追加する。

        ビューアのスナップと同じ保留中添付リストに積むので、ビューアの
        スナップと混ぜてまとめてLLMへ送信できる。
        """
        if not png:
            self._append_error("カメラから空の画像が届きました。")
            return
        self._snap_counter += 1
        label = (
            f"カメラスナップ{self._snap_counter} ({note})" if note
            else f"カメラスナップ{self._snap_counter}"
        )
        thumb = _png_to_thumb(png, 72)
        if thumb is None:
            self._append_error("カメラスナップのエンコードに失敗しました。")
            self._snap_counter -= 1
            return
        self._pending_images.append(
            {"path": "", "png": png, "thumb": thumb, "label": label,
             "source": "camera"}
        )
        self._rebuild_attach_strip()
        self._append_meta(f"📷 カメラスナップを添付しました: {label}")

    def _remove_pending(self, index: int) -> None:
        if 0 <= index < len(self._pending_images):
            self._pending_images.pop(index)
            self._rebuild_attach_strip()

    def _clear_pending(self) -> None:
        self._pending_images.clear()
        self._snap_counter = 0
        self.attach_strip.setVisible(False)

    def _save_attachment_png(self, label: str, png: bytes) -> str | None:
        """送信画像を ~/.pathology_app/attachments/ へPNG保存しパスを返す。

        ファイル名は「{症例ID}_{日時}_{連番}_{ラベル}.png」。症例JSONには
        このパスだけを記録する (base64埋め込みによる肥大化を防ぐ)。
        保存に失敗したときはNoneを返し、呼び出し側はbase64埋め込みに
        フォールバックする。
        """
        try:
            ensure_dirs()
            ts = time.strftime("%Y%m%d-%H%M%S")
            stem = f"{self._case_id or 'case'}_{ts}_{self._attach_seq:02d}"
            slug = _attachment_slug(label)
            path = os.path.join(ATTACHMENTS_DIR, f"{stem}_{slug}.png")
            n = 1
            while os.path.exists(path):
                path = os.path.join(ATTACHMENTS_DIR, f"{stem}_{slug}_{n}.png")
                n += 1
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(png)
            os.replace(tmp, path)
            self._attach_seq += 1
            return path
        except Exception:
            return None

    def _rebuild_attach_strip(self) -> None:
        """保留中の添付画像のサムネイル帯を作り直す。"""
        while self.attach_lay.count():
            it = self.attach_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for i, item in enumerate(self._pending_images):
            self.attach_lay.addWidget(self._make_chip(i, item))
        self.attach_lay.addStretch(1)
        self.attach_strip.setVisible(bool(self._pending_images))

    def _make_chip(self, index: int, item: dict) -> QWidget:
        """添付画像1枚分のサムネイル + 解除ボタンのチップを作る。"""
        name = self._item_label(item)
        if item.get("path"):
            tooltip = item["path"]
        elif item.get("source") == "camera":
            tooltip = "顕微鏡カメラのスナップショット"
        else:
            tooltip = "ビューアのスナップショット"
        chip = QFrame()
        chip.setStyleSheet(
            "QFrame{background:#242b38;border:1px solid #3d4757;border-radius:6px;}"
        )
        cl = QVBoxLayout(chip)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(2)
        icon = QLabel()
        icon.setPixmap(item["thumb"])
        icon.setFixedSize(72, 72)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setToolTip(tooltip)
        btn = QPushButton(f"✕ {name}")
        btn.setToolTip("この添付画像を解除")
        btn.setFlat(True)
        btn.setMaximumWidth(88)
        btn.setStyleSheet(
            "QPushButton{padding:0 2px;font-size:10px;}"
            "QPushButton:hover{color:#ff8a80;}"
        )
        btn.clicked.connect(lambda _=False, idx=index: self._remove_pending(idx))
        cl.addWidget(icon, 0, Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(btn)
        return chip

    # ---------------------------------------------------------- drag&drop
    def dragEnterEvent(self, ev) -> None:
        urls = ev.mimeData().urls()
        if urls and _image_paths_in_urls(urls):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dropEvent(self, ev) -> None:
        """パネル自体へのドロップ: 画像は添付に追加する。"""
        paths = _image_paths_in_urls(ev.mimeData().urls())
        ev.acceptProposedAction()
        if paths:
            self._add_images_from_paths(paths)

    # ------------------------------------------------------------ rendering
    def _insert_html(self, html: str) -> None:
        cursor = self.browser.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertHtml(html)
        self.browser.setTextCursor(cursor)
        self.browser.verticalScrollBar().setValue(
            self.browser.verticalScrollBar().maximum()
        )

    def _append_meta(self, text: str) -> None:
        self._insert_html(f'<span class="meta">{_esc(text)}</span><br>')

    def _append_user(self, text: str) -> None:
        self._insert_html(
            f'<span class="user"><b>👤 病理医:</b></span><br>{_esc(text)}<br><br>'
        )

    def _append_error(self, text: str) -> None:
        self._insert_html(f'<span class="err"><b>⚠ エラー:</b> {_esc(text)}</span><br><br>')

    def _stream_append(self, piece: str) -> None:
        cursor = self.browser.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(piece)
        self.browser.setTextCursor(cursor)
        sb = self.browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    def load_messages(self, messages: list[dict]) -> None:
        """保存された履歴を表示する。添付画像はサムネイルとして本文の下に
        表示する (過去の症例を開いても画像が見えるようにする)。"""
        self.browser.clear()
        self.messages = [dict(m) for m in messages if isinstance(m, dict)]
        self._emb_seq = 0
        for m in self.messages:
            content = m.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
                n_img = sum(
                    1 for p in content if isinstance(p, dict)
                    and p.get("type") == "image_url"
                )
                img_html = self._attachments_html(content)
            else:
                text, n_img, img_html = (content or ""), 0, ""
            if m.get("role") == "user":
                head = text or (f"[画像x{n_img}を添付]" if n_img else "")
                self._append_user(head)
                if img_html:
                    self._insert_html(img_html)
                elif n_img:
                    self._append_meta(
                        f"[画像x{n_img}を添付] (添付ファイルが見つかりません)"
                    )
            elif m.get("role") == "assistant":
                self._insert_html('<span class="ai"><b>🤖 AI:</b></span><br>')
                self._stream_append(text)
                self._insert_html("<br><br>")
        sb = self.browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _attachments_html(self, content: list) -> str:
        """userメッセージのimage_urlパートをサムネイル表示のHTMLにする。

        パス参照はそのパスのPNGを、旧形式 (base64埋め込み) はdata URLを
        デコードしてQPixmapを登録し、本文中の <img> で表示する。
        """
        doc = self.browser.document()
        pieces: list[str] = []
        for p in content:
            if not (isinstance(p, dict) and p.get("type") == "image_url"):
                continue
            iu = p.get("image_url") or {}
            pm = self._attachment_pixmap(iu)
            name = _attachment_display_name(iu)
            if pm is None:
                pieces.append(
                    f'<span class="meta">🖼 {_esc(name)} '
                    f'(添付ファイルが見つかりません)</span><br>'
                )
                continue
            key = QUrl(f"attachimg{self._emb_seq}")
            self._emb_seq += 1
            doc.addResource(
                int(QTextDocument.ResourceType.ImageResource), key, pm
            )
            pieces.append(
                f'<img src="{key.toString()}" width="{pm.width()}" '
                f'height="{pm.height()}">'
                f'<br><span class="meta">🖼 {_esc(name)}</span><br>'
            )
        return "".join(pieces)

    def _attachment_pixmap(self, iu: dict) -> QPixmap | None:
        """image_urlパートからサムネイルQPixmapを作る (失敗時はNone)。

        パス参照・旧形式のbase64 data URLのどちらにも対応する。
        表示サイズは長辺260pxに抑える (履歴が長い場合の負荷対策)。
        """
        path = iu.get("path") or ""
        img = QImage()
        if path:
            img = QImage(path)
        if img.isNull():
            data = _data_url_png(iu.get("url") or "")
            if data is not None:
                img = QImage()
                img.loadFromData(data)
        if img.isNull():
            return None
        pm = QPixmap.fromImage(img)
        m = max(pm.width(), pm.height())
        if m > 260:
            pm = pm.scaled(
                260, 260,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return pm

    def clear_chat(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.messages = []
        self.browser.clear()
        self.messages_changed.emit()

    # ------------------------------------------------------------ sending
    def _on_send_clicked(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            return
        text = self.input.toPlainText().strip()
        if text or self._pending_images:
            self._send_user_text()

    def _get_context(self) -> dict:
        ctx = {"case_info": "(情報なし)", "notes": "", "png": None}
        if self.context_getter:
            try:
                ctx.update(self.context_getter() or {})
            except Exception as e:
                # 臨床情報などの取得失敗を握りつぶさずUIに通知する
                self._append_error(f"コンテキスト (臨床情報・メモ) の取得に失敗: {e}")
        return ctx

    def _compose_content(self, text: str, ctx: dict):
        """送信コンテンツを組み立てる。複数画像に対応。

        「画像を添付」チェック時はビューPNG、それに加えて保留中の添付画像
        (スナップショット・「＋ 画像…」・ドロップで追加したもの) をすべて
        image_url パートとして連結する。2枚以上のときは画像の並び順と
        ラベル (スナップ番号・倍率・ファイル名) を本文に注記する。

        画像は送信のたびに ~/.pathology_app/attachments/ へPNG保存し、
        content にはパス参照 ({"path": ...}) だけを入れる。症例JSONへの
        base64埋め込みを防ぎ、後から画像ファイルを直接開けるようにする。
        LLMへの送信時は _llm_messages() がパス参照をbase64 data URLへ
        変換してから渡す。

        戻り値: (content, 添付枚数)
        """
        images: list[tuple[str, bytes]] = []
        png = ctx.get("png") if (self.chk_attach.isChecked() and ctx.get("png")) else None
        if png:
            images.append(("現在のビュー画像", png))
        for item in self._pending_images:
            images.append((self._item_label(item), item["png"]))
        if not images:
            return text, 0
        self._attach_seq = 0  # 同一送信バッチ内の連番をリセット
        content: list[dict] = [{"type": "text", "text": text}]
        if len(images) > 1:
            lines = "\n".join(f"{i + 1}. {n}" for i, (n, _) in enumerate(images))
            content.append({
                "type": "text",
                "text": (
                    "(添付画像は上記の番号順で含まれます。スナップには"
                    "おおよその実効倍率と画像全体の中での中心位置が付いています)"
                    f"\n{lines}"
                ),
            })
        for label, img in images:
            path = self._save_attachment_png(label, img)
            if path:
                content.append({
                    "type": "image_url",
                    "image_url": {"path": path, "label": label},
                })
            else:
                # 保存できない環境では従来どおりbase64で埋め込む
                b64 = base64.b64encode(img).decode("ascii")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
        return content, len(images)

    def _send_user_text(self) -> None:
        """入力欄の自由文を送信する (添付画像・チェック状態をまとめて送る)。

        標本情報・臨床情報 (部位など)・ユーザーメモは system メッセージとして
        毎回LLMへ渡す (保存する履歴には本文のみ記録し、表示は簡潔に保つ)。
        テキストが空でも添付画像があれば送信できる。
        """
        text = self.input.toPlainText().strip()
        if not text and not self._pending_images:
            return
        ctx = self._get_context()
        if not text:
            text = "添付した画像について検討してください。"
        content, n_att = self._compose_content(text, ctx)
        self.input.clear()
        display = text + (f" [画像x{n_att}を添付]" if n_att else "")
        self._dispatch(
            {"role": "user", "content": content},
            display=display,
            with_context=True,
            ctx=ctx,
        )

    def send_prompt(self, kind: str) -> None:
        """所見 / 鑑別診断ボタン。入力欄のテキストは補足指示として一緒に送る。"""
        if self.client is None:
            self._append_error("LLMクライアントが初期化されていません。")
            return
        if self.worker is not None and self.worker.isRunning():
            return
        ctx = self._get_context()
        supplement = self.input.toPlainText().strip()

        if kind == "findings":
            label = "所見の検討"
            prompt = prompts.findings_prompt(ctx["case_info"], ctx["notes"])
        elif kind == "differential":
            label = "鑑別診断の検討"
            prompt = prompts.differential_prompt(ctx["case_info"], ctx["notes"])
        else:
            self._append_error(f"未知のプロンプト種別: {kind}")
            return

        if supplement:
            prompt += (
                f"\n\n# ユーザーからの補足指示 (必ず考慮してください)\n{supplement}\n"
            )

        content, n_att = self._compose_content(prompt, ctx)
        display = f"[{label}]"
        if supplement:
            display += f" 補足: {supplement}"
        if n_att:
            display += f" [画像x{n_att}を添付]"
        self.input.clear()

        # 所見/鑑別プロンプトは ctx を本文に埋め込むため、ここでは重複注入しない
        self._dispatch({"role": "user", "content": content}, display=display)

    def _dispatch(
        self,
        user_msg: dict,
        display: str | None = None,
        with_context: bool = False,
        ctx: dict | None = None,
    ) -> None:
        """ユーザーメッセージを確定してLLMストリーミングを開始する。

        with_context=True のとき (自由チャット)、現在の標本情報・臨床情報・
        ユーザーメモを system メッセージに含めて送る。履歴 (self.messages) に
        は本文のみ保存するため、保存後の再読込でも毎回最新のコンテキストが
        注入される。
        """
        if self.client is None:
            self._append_error("LLMクライアントが初期化されていません。")
            return
        msg = dict(user_msg)
        # 表示用テキスト (所見/鑑別ラベル・添付注記を含む) を自動タイトルの
        # 材料として症例JSONにも保存する (本文と同一なら不要)
        if display and display != _content_text(user_msg.get("content")):
            msg["display"] = display
        self.messages.append(msg)
        self._append_user(display or _content_text(user_msg.get("content")))
        self._insert_html('<span class="ai"><b>🤖 AI:</b></span> ')
        self._stream_accum = []

        system_prompt = prompts.SYSTEM_PROMPT
        if with_context and ctx is not None:
            system_prompt += (
                "\n\n"
                + prompts.case_info_block(ctx.get("case_info", "(情報なし)"))
                + "\n"
                + prompts.notes_block(ctx.get("notes", ""))
            )
        # 保存用メッセージ (パス参照) をLLM用 (base64 data URL) に変換して送る
        base = [{"role": "system", "content": system_prompt}] \
            + self._llm_messages(self.messages)
        self._clear_pending()  # 送信済み添付を解除 (次の送信には含めない)
        self._start_worker(base)

    def _llm_messages(self, messages: list[dict]) -> list[dict]:
        """保存用メッセージをLLM用に変換する (パス参照 → base64 data URL)。

        症例JSONにはパスだけを保存しているため、LLMへ送るときは添付PNGを
        読み直してdata URLへ組み立てる。旧形式 (base64埋め込み) のメッセージ
        はそのまま透過する。画像ファイルが読めない場合もそのまま渡す。
        """
        out: list[dict] = []
        for m in messages:
            content = m.get("content")
            if not isinstance(content, list):
                out.append({"role": m.get("role"), "content": content})
                continue
            parts: list = []
            for p in content:
                if not (isinstance(p, dict) and p.get("type") == "image_url"):
                    parts.append(p)
                    continue
                iu = p.get("image_url") or {}
                path = iu.get("path") or ""
                data = None
                if path:
                    try:
                        with open(path, "rb") as f:
                            data = f.read()
                    except Exception:
                        data = None
                if data is not None:
                    b64 = base64.b64encode(data).decode("ascii")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    })
                else:
                    parts.append(p)
            # LLMペイロードは role/content のみ (display 等の保存用メタは外す)
            out.append({"role": m.get("role"), "content": parts})
        return out

    def _start_worker(self, messages: list[dict]) -> None:
        """LLMストリーミングスレッドを開始する (テストで差し替え可能)。"""
        self.worker = ChatWorker(self.client, messages)
        self.worker.chunk.connect(self._on_chunk)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self._set_busy(True)
        self.worker.start()

    def _on_chunk(self, piece: str) -> None:
        self._stream_accum.append(piece)
        self._stream_append(piece)

    def _on_finished(self, full_text: str) -> None:
        text = "".join(self._stream_accum) or full_text
        self._stream_accum = []
        self.messages.append({"role": "assistant", "content": text})
        self._insert_html("<br><br>")
        self._set_busy(False)
        self.worker = None
        self.messages_changed.emit()

    def _on_failed(self, message: str) -> None:
        self._stream_accum = []
        self._append_error(message)
        self._set_busy(False)
        self.worker = None
        # 失敗時もユーザー発言を履歴・保存対象に反映する
        self.messages_changed.emit()


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return next((p.get("text", "") for p in content if p.get("type") == "text"), "")
    return ""
