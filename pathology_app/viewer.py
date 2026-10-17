"""WSI / 通常画像ビューア。

- .svs / .ndpi / .mrxs / .bif / .vsi / .scn / ピラミッドtiff は OpenSlide 経由
  (ピラミッドレベルを生かした低メモリなリージョン描画)
- それ以外 (png/jpg/bmp/通常tiff など) は Pillow 経由で QImage 化

操作: ドラッグ=パン / ホイール=ズーム(カーソル位置固定) /
      Shift+ホイール・横スクロール=パン / ダブルクリック=全体表示 /
      + - 0 矢印キー
"""
from __future__ import annotations

import math
import os

from PySide6.QtCore import QByteArray, QBuffer, Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

try:
    import openslide
    HAS_OPENSLIDE = True
except Exception:  # ImportError だけでなく native lib のロード失敗も拾う
    HAS_OPENSLIDE = False

WSI_EXTS = {".svs", ".ndpi", ".mrxs", ".bif", ".vsi", ".scn", ".tif", ".tiff", ".qptiff"}

MAX_REGION_PIXELS = 4096  # 1回のread_regionで読む最大辺 (メモリ上限保護)
MAX_ZOOM = 40.0           # 画面px / スライドpx の上限


class SlideDocument:
    """WSI と通常画像を同一インターフェースで扱う。"""

    def __init__(self, path: str):
        self.path = path
        self.width = 0
        self.height = 0
        self.format_name = ""
        self.magnification = "?"
        self.level_ds: list[float] = [1.0]
        self.slide = None        # openslide.OpenSlide
        self.qimg = None         # QImage (通常画像)
        self._buf = None         # QImageが参照する生バッファ(生存保証)
        self.pil_full = None

        ext = os.path.splitext(path)[1].lower()
        self.format_name = ext.lstrip(".").upper() or "?"
        if HAS_OPENSLIDE and ext in WSI_EXTS:
            try:
                slide = openslide.OpenSlide(path)
                self.slide = slide
                self.width, self.height = slide.dimensions
                self.level_ds = [
                    float(slide.level_downsamples[i]) for i in range(slide.level_count)
                ]
                props = slide.properties
                self.magnification = props.get(openslide.PROPERTY_NAME_OBJECTIVE_POWER, "?")
                self.format_name = props.get(
                    openslide.PROPERTY_NAME_VENDOR, "WSI"
                ) or "WSI"
            except Exception:
                # OpenSlideが読めないtiff等 → 通常画像として再試行
                self.slide = None
        if self.slide is None:
            from PIL import Image
            im = Image.open(path)
            try:
                # アニメーションtiff等は最初のフレームに固定
                im.seek(0)
            except Exception:
                pass
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            self.width, self.height = im.size
            self.pil_full = im
            self._buf = im.tobytes("raw", "RGBA")
            self.qimg = QImage(self._buf, im.width, im.height, im.width * 4,
                               QImage.Format_RGBA8888)
            self.level_ds = [1.0]

    @property
    def is_wsi(self) -> bool:
        return self.slide is not None

    def read_region(self, x: int, y: int, level: int, w: int, h: int) -> QImage:
        """(level0座標x,y) を左上に、level解像度で w×h 読み出し QImage で返す。"""
        if self.slide is not None:
            img = self.slide.read_region((x, y), level, (max(1, w), max(1, h)))
            buf = img.tobytes("raw", "RGBA")
            q = QImage(buf, img.width, img.height, img.width * 4, QImage.Format_RGBA8888)
            return q.copy()  # Qt所有メモリに複製してバッファ生存問題を回避
        raise RuntimeError("通常画像では read_region は使用しません")


class SlideViewer(QWidget):
    """ズーム・パン可能な画像ビューア。"""
    imageLoaded = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.doc: SlideDocument | None = None
        self.load_error = ""
        # center: スライド座標での表示中心 / zoom: 画面px / スライドpx
        self.center = QPointF(0.0, 0.0)
        self.zoom = 1.0
        self._drag_prev: QPointF | None = None

    # ------------------------------------------------------------------ load
    def load(self, path: str) -> None:
        try:
            doc = SlideDocument(path)
        except Exception as e:
            self.doc = None
            self.load_error = f"読み込みに失敗しました: {e}"
            self.imageLoaded.emit(path)
            self.update()
            return
        self.load_error = ""
        self.doc = doc
        self._fit()
        self.setCursor(Qt.OpenHandCursor)
        self.imageLoaded.emit(path)
        self.update()

    def close_image(self) -> None:
        self.doc = None
        self.load_error = ""
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def _fit(self) -> None:
        if self.doc is None:
            return
        w = max(1, self.width())
        h = max(1, self.height())
        self.zoom = min(w / self.doc.width, h / self.doc.height)
        self.zoom = max(min(self.zoom, MAX_ZOOM), 1e-4)
        self.center = QPointF(self.doc.width / 2.0, self.doc.height / 2.0)

    # ------------------------------------------------------------ level選択
    def _choose_level(self) -> int:
        """表示解像度 (zoom) に最も合うピラミッドレベルを選ぶ。

        画面px/スライドpx = zoom。必要な縮小率は 1/zoom。
        その縮小率以下の最大レベル (読みすぎ回避・品位確保) を選ぶ。
        """
        doc = self.doc
        if doc is None or not doc.is_wsi:
            return 0
        target = 1.0 / max(self.zoom, 1e-4)
        best = 0
        for i, ds in enumerate(doc.level_ds):
            if ds <= target * 1.01:
                best = i
        return best

    # ------------------------------------------------------------- painting
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(16, 16, 20))
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        if self.doc is None:
            p.setPen(QColor(120, 130, 150))
            p.setFont(QFont("sans-serif", 13))
            msg = self.load_error or "画像を開いてください (Ctrl+O / 「開く」ボタン)"
            p.drawText(self.rect(), Qt.AlignCenter, msg)
            p.end()
            return

        doc = self.doc
        w = self.width()
        h = self.height()

        if doc.is_wsi:
            self._paint_wsi(p, doc, w, h)
        else:
            self._paint_plain(p, doc, w, h)

        self._paint_overlay(p)
        p.end()

    def _visible_slide_rect(self, w: int, h: int) -> QRectF:
        cx = self.center.x() - (w / 2.0) / self.zoom
        cy = self.center.y() - (h / 2.0) / self.zoom
        return QRectF(cx, cy, w / self.zoom, h / self.zoom)

    def _region_for_view(self):
        """現在の表示に必要なリージョンを計算する。

        戻り値: (x0, y0, level, rw, rh) or None
        - x0, y0: リージョン左上の **level-0座標** (read_region の location は
          常に level-0 基準なので、ここで変換して渡すのが正しい使い方)
        - level: 使用するピラミッドレベル
        - rw, rh: そのレベルでの読み出しピクセル数

        テスト可能性を上げるため描画から分離している。
        """
        doc = self.doc
        if doc is None or not doc.is_wsi:
            return None
        w = max(1, self.width())
        h = max(1, self.height())
        vis = self._visible_slide_rect(w, h)
        # 表示領域がスライド外なら描画不要
        if (vis.right() <= 0 or vis.bottom() <= 0
                or vis.left() >= doc.width or vis.top() >= doc.height):
            return None
        level = self._choose_level()
        ds = doc.level_ds[level]
        x0 = int(max(0, min(math.floor(vis.left()), doc.width - 1)))
        y0 = int(max(0, min(math.floor(vis.top()), doc.height - 1)))
        # レベル-0の可視幅をそのレベルのピクセル数へ変換 (端数分切り上げ)
        rw = int(math.ceil((min(vis.right(), doc.width) - x0) / ds))
        rh = int(math.ceil((min(vis.bottom(), doc.height) - y0) / ds))
        rw = max(1, min(rw, MAX_REGION_PIXELS))
        rh = max(1, min(rh, MAX_REGION_PIXELS))
        return x0, y0, level, rw, rh

    def _paint_wsi(self, p: QPainter, doc: SlideDocument, w: int, h: int) -> None:
        vis = self._visible_slide_rect(w, h)
        region = self._region_for_view()
        if region is None:
            return
        x0, y0, level, rw, rh = region
        ds = doc.level_ds[level]
        try:
            # location は level-0 座標、size はそのレベルのピクセル数
            qimg = doc.read_region(x0, y0, level, rw, rh)
        except Exception as e:
            p.setPen(QColor(255, 90, 90))
            p.drawText(self.rect(), Qt.AlignCenter, f"リージョン読み込みエラー: {e}")
            return
        # 読み出した領域をスライド座標上の正しい位置へ配置する
        dev_x = (x0 - vis.left()) * self.zoom
        dev_y = (y0 - vis.top()) * self.zoom
        target = QRectF(
            dev_x, dev_y,
            qimg.width() * ds * self.zoom, qimg.height() * ds * self.zoom,
        )
        p.drawImage(target, qimg)

    def _paint_plain(self, p: QPainter, doc: SlideDocument, w: int, h: int) -> None:
        # スライド座標→デバイス座標の変換で全体を描く
        p.save()
        cx = w / 2.0 - self.center.x() * self.zoom
        cy = h / 2.0 - self.center.y() * self.zoom
        p.translate(cx, cy)
        p.scale(self.zoom, self.zoom)
        p.drawImage(0, 0, doc.qimg)
        p.restore()

    def _paint_overlay(self, p: QPainter) -> None:
        doc = self.doc
        p.setPen(QColor(220, 225, 240))
        font = QFont("monospace", 10)
        p.setFont(font)
        lines = [
            f"{os.path.basename(doc.path)}   "
            f"{doc.format_name}   {doc.width}×{doc.height}px"
            + (f"  倍率{doc.magnification}x" if doc.magnification != "?" else ""),
            f"ズーム {(self.zoom * 100):.1f}%"
            + (f"  (レベル{self._choose_level()}/{len(doc.level_ds) - 1})" if doc.is_wsi else ""),
        ]
        y = 14
        fm_w = max(p.fontMetrics().horizontalAdvance(t) for t in lines) + 16
        p.fillRect(0, 0, fm_w, 14 + 16 * len(lines) + 6, QColor(10, 10, 14, 170))
        for line in lines:
            p.drawText(8, y + 12, line)
            y += 16

    # ------------------------------------------------------------ interaction
    def _slide_at(self, pos: QPointF) -> QPointF:
        w = self.width()
        h = self.height()
        return QPointF(
            self.center.x() + (pos.x() - w / 2.0) / self.zoom,
            self.center.y() + (pos.y() - h / 2.0) / self.zoom,
        )

    def _pan_by(self, dx: float, dy: float) -> None:
        """画面座標 dx,dy 分だけパンする。"""
        if self.doc is None or (dx == 0 and dy == 0):
            return
        self.center = QPointF(self.center.x() - dx / self.zoom,
                              self.center.y() - dy / self.zoom)
        self._clamp_center()
        self.update()

    def mousePressEvent(self, ev) -> None:
        if ev.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._drag_prev = QPointF(ev.position())
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()

    def mouseReleaseEvent(self, ev) -> None:
        if self._drag_prev is not None:
            self._drag_prev = None
            self.setCursor(Qt.OpenHandCursor if self.doc else Qt.ArrowCursor)
            ev.accept()

    def mouseMoveEvent(self, ev) -> None:
        if self.doc is None:
            return
        if self._drag_prev is None:
            # ホバー中は開いた手のカーソルにしてドラッグ可能を示す
            if not (ev.buttons() & (Qt.LeftButton | Qt.MiddleButton)):
                self.setCursor(Qt.OpenHandCursor)
            return
        pos = QPointF(ev.position())
        dx = pos.x() - self._drag_prev.x()
        dy = pos.y() - self._drag_prev.y()
        self._drag_prev = pos
        self._pan_by(dx, dy)

    def wheelEvent(self, ev) -> None:
        """ホイール操作。

        - 縦ホイール: ズーム (カーソル位置固定)
        - Shift+縦ホイール: 縦パン
        - 横ホイール / pixelDelta(タッチパッド): パン
        """
        if self.doc is None:
            return
        ad = ev.angleDelta()
        pd = ev.pixelDelta()
        mods = ev.modifiers()

        # タッチパッドの2本指スクロール (pixelDeltaのみ) → パン
        if not (mods & Qt.ControlModifier) and (pd.x() or pd.y()) and not ad:
            self._pan_by(pd.x(), pd.y())
            return
        # 横ホイール → 横パン
        if ad.x() and not ad.y():
            self._pan_by(-ad.x() * 2.0, 0)
            return
        # Shift+縦ホイール → 縦パン
        if (mods & Qt.ShiftModifier) and ad.y():
            self._pan_by(0, -ad.y() * 2.0)
            return

        steps = ad.y() / 120.0
        if steps == 0:
            return
        factor = 1.25 ** steps
        cursor = QPointF(ev.position())
        slide_before = self._slide_at(cursor)
        new_zoom = max(min(self.zoom * factor, MAX_ZOOM), 1e-4)
        if abs(new_zoom - self.zoom) < 1e-9:
            return
        self.zoom = new_zoom
        w = self.width()
        h = self.height()
        # カーソル下のスライド点をカーソル位置に固定する
        self.center = QPointF(
            slide_before.x() - (cursor.x() - w / 2.0) / self.zoom,
            slide_before.y() - (cursor.y() - h / 2.0) / self.zoom,
        )
        self._clamp_center()
        self.update()

    def mouseDoubleClickEvent(self, ev) -> None:
        if self.doc is not None:
            self._fit()
            self.update()

    def keyPressEvent(self, ev) -> None:
        if self.doc is None:
            super().keyPressEvent(ev)
            return
        # 1回のキーでビューポートの約15%移動
        step = max(40.0, min(self.width(), self.height()) * 0.15) / self.zoom
        if ev.key() == Qt.Key_Plus or ev.key() == Qt.Key_Equal:
            self.zoom = max(min(self.zoom * 1.25, MAX_ZOOM), 1e-4)
        elif ev.key() == Qt.Key_Minus:
            self.zoom = max(min(self.zoom / 1.25, MAX_ZOOM), 1e-4)
        elif ev.key() == Qt.Key_0:
            self._fit()
        elif ev.key() == Qt.Key_Left:
            self.center = QPointF(self.center.x() - step, self.center.y())
        elif ev.key() == Qt.Key_Right:
            self.center = QPointF(self.center.x() + step, self.center.y())
        elif ev.key() == Qt.Key_Up:
            self.center = QPointF(self.center.x(), self.center.y() - step)
        elif ev.key() == Qt.Key_Down:
            self.center = QPointF(self.center.x(), self.center.y() + step)
        else:
            super().keyPressEvent(ev)
            return
        self._clamp_center()
        self.update()

    def _clamp_center(self) -> None:
        doc = self.doc
        if doc is None:
            return
        # 画面の半分以上外に出ないよう軽くクランプ
        half_w = (self.width() / 2.0) / self.zoom
        half_h = (self.height() / 2.0) / self.zoom
        lo_x, hi_x = half_w, doc.width - half_w
        lo_y, hi_y = half_h, doc.height - half_h
        cx = min(max(self.center.x(), lo_x), hi_x) if hi_x > lo_x else doc.width / 2.0
        cy = min(max(self.center.y(), lo_y), hi_y) if hi_y > lo_y else doc.height / 2.0
        self.center = QPointF(cx, cy)

    # ------------------------------------------------------ export for LLM
    def export_current_view(self, max_px: int = 1280) -> bytes | None:
        """現在表示中のビューをPNG (bytes) として書き出す。"""
        if self.doc is None:
            return None
        pixmap = self.grab()  # QWidgetの描画をそのまま取得
        scaled = pixmap.scaled(max_px, max_px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        img = scaled.toImage()
        arr = QByteArray()
        qbuf = QBuffer(arr)
        qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
        if not img.save(qbuf, "PNG"):
            return None
        return bytes(arr)
