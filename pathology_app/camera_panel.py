"""カメラパネル: Swiftcam (顕微鏡カメラ) のライブプレビューとスナップ添付。

device操作は1スレッド規約のため、すべての SwiftcamCamera 呼び出しを
PreviewWorker (QThread) に封入する。GUI とはシグナルでやり取りする:
  frame_ready(bytes RGB24, w, h)   プレビューフレーム
  still_ready(png bytes, note)     スナップ撮像完了 (PNG化済み)
  error(str)                       失敗通知
"""
from __future__ import annotations

import queue
import time

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import camera
from camera import CameraError, SwiftcamCamera


def _to_pixmap(data: bytes, w: int, h: int) -> QPixmap:
    """RGB24バイト列をQPixmapへ (スケーリングは表示側で)。"""
    img = QImage(data, w, h, w * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(img)


class PreviewWorker(QThread):
    """SwiftcamCameraを封入するワーカースレッド (デバイス操作はここだけ)。"""

    frame_ready = Signal(bytes, int, int)
    still_ready = Signal(bytes, str)
    error = Signal(str)
    state = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cam: SwiftcamCamera | None = None
        self._q: queue.Queue = queue.Queue()
        self._quit = False

    # ------------------------------------------------ GUI側からのAPI (スレッド安全)
    def post(self, cmd, payload=None) -> None:
        self._q.put((cmd, payload))

    def quit_loop(self) -> None:
        self._quit = True
        self._q.put(("quit", None))

    # ------------------------------------------------ ワーカースレッド本体
    def run(self) -> None:  # noqa: C901 (コマンドディスパッチの直列処理)
        try:
            cams = camera.list_cameras()
            if not cams:
                self.error.emit("Swiftcamカメラが見つかりません (USB接続を確認してください)")
                return
            cam = SwiftcamCamera()
            cam.open()
            self.cam = cam
            self.state.emit(f"📹 {cam.displayname} 接続")
            running = False
            while not self._quit:
                # コマンド処理 (すべてこのスレッドからデバイス操作する)
                cmd = payload = None
                try:
                    cmd, payload = self._q.get(timeout=0.02)
                except queue.Empty:
                    pass
                if cmd == "quit":
                    break
                elif cmd == "start":
                    try:
                        # 選択解像度がカメラの現在サイズと違うなら一度止めて
                        # 変更してから再開する
                        if payload and tuple(cam.size()) != tuple(payload):
                            cam.stop()
                            cam.set_size(*payload)
                            running = False
                        if not running:
                            cam.start()
                            running = True
                            self.state.emit("ライブプレビュー実行中")
                    except CameraError as e:
                        self.error.emit(str(e))
                elif cmd == "stop":
                    if running:
                        cam.stop()
                        running = False
                        self.state.emit("ライブプレビュー停止")
                elif cmd == "setsize":
                    try:
                        cam.stop()
                        cam.set_size(*payload)
                        # サイズ変更後はライブを続ける
                        cam.start()
                        running = True
                    except CameraError as e:
                        self.error.emit(str(e))
                elif cmd == "setexpo":
                    try:
                        cam.set_exposure(int(payload))
                    except CameraError as e:
                        self.error.emit(str(e))
                elif cmd == "setgain":
                    try:
                        cam.set_gain(int(payload))
                    except CameraError as e:
                        self.error.emit(str(e))
                elif cmd == "snap":
                    try:
                        data, w, h = cam.snap_still()
                        self.state.emit("スチル撮影完了")
                        # PNG化 (LLM送信用に長辺1280pxへ縮小) もワーカーで
                        png = SwiftcamCamera.png_from_frame(data, w, h)
                        if png:
                            us = cam.get_exposure()
                            exp = (
                                f" 露出{us / 1000:.0f}ms" if us > 0
                                else " (露出オート)"
                            )
                            note = f"{w}×{h}{exp}"
                            self.still_ready.emit(png, note)
                        else:
                            self.error.emit("スチル画像のエンコードに失敗しました")
                    except CameraError as e:
                        self.error.emit(str(e))
                    except Exception as e:
                        self.error.emit(f"スチル撮影に失敗: {e}")
                elif cmd == "still_full":
                    try:
                        self.state.emit("フル解像度で撮影中…")
                        data, w, h = cam.full_res_still()
                        png = SwiftcamCamera.png_from_frame(data, w, h)
                        if png:
                            self.state.emit("フル解像度スチル完了")
                            note = f"{w}×{h} (フル解像度)"
                            self.still_ready.emit(png, note)
                        else:
                            self.error.emit("フル解像度スチルの撮影に失敗しました")
                    except CameraError as e:
                        self.error.emit(str(e))
                    except Exception as e:
                        self.error.emit(f"フル解像度スチルに失敗: {e}")

                # プレビューフレーム取得
                if running and cmd is None:
                    got = cam.wait_frame(500)
                    if got:
                        self.frame_ready.emit(got[0], got[1], got[2])
            try:
                cam.close()
            except CameraError:
                pass
            self.state.emit("カメラ切断")
        except CameraError as e:
            self.error.emit(str(e))
            self.state.emit("カメラ未接続")
        except Exception as e:
            self.error.emit(f"カメラ処理でエラー: {e}")


class CameraPanel(QWidget):
    """顕微鏡カメラのライブプレビュー + スナップ添付パネル。"""

    snapshot_ready = Signal(bytes, str)  # (png, note)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: PreviewWorker | None = None
        self._frames = 0
        self._fps_t0 = time.time()

        self.preview = QLabel("カメラ未接続")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(360, 270)
        self.preview.setStyleSheet("background:#10141b;")

        self.status = QLabel("—")
        self.btn_live = QPushButton("🎥 ライブ")
        self.btn_live.setCheckable(True)
        self.btn_live.setToolTip("ライブプレビューの開始・停止")

        self.cmb_size = QComboBox()
        for w, h in camera.PREVIEW_SIZES:
            self.cmb_size.addItem(f"{w}×{h}", (w, h))
        self.cmb_size.setCurrentIndex(0)
        self.cmb_size.setToolTip("プレビュー解像度 (高いほど拡大しても精細。スチルもこの解像度)")

        self.spn_expo = QSpinBox()
        self.spn_expo.setRange(200, 2000000)
        self.spn_expo.setSingleStep(5000)
        self.spn_expo.setSuffix(" µs")
        self.spn_expo.setValue(70000)
        self.spn_expo.setToolTip("露光時間 (µs)。暗い視野では大きくする")
        self.spn_gain = QSpinBox()
        self.spn_gain.setRange(100, 5000)
        self.spn_gain.setSingleStep(100)
        self.spn_gain.setValue(100)
        self.spn_gain.setToolTip("ゲイン (感度)。大きくすると明るくなるがノイズ増")

        self.chk_full = QCheckBox("フル解像度で撮る")
        self.chk_full.setToolTip(
            "スナップ時に最高解像度 (3664×2748) で撮影します。"
            "通常はプレビュー解像度のまま (高速) で十分です。"
        )
        self.btn_snap = QPushButton("📷 スナップを添付")
        self.btn_snap.setToolTip(
            "カメラのスチルを撮影してチャットの添付に追加します。"
            "複数枚撮り溜めてまとめてLLMへ送れます。"
        )
        self.btn_save = QPushButton("💾 保存…")
        self.btn_save.setToolTip("現在のプレビュー解像度でPNG保存します")

        self.fps = QLabel("")

        head = QHBoxLayout()
        head.addWidget(self.btn_live)
        head.addWidget(self.cmb_size)
        head.addWidget(self.spn_expo)
        head.addWidget(self.spn_gain)
        head.addStretch(1)

        still_row = QHBoxLayout()
        still_row.addWidget(self.btn_snap)
        still_row.addWidget(self.chk_full)
        still_row.addWidget(self.btn_save)
        still_row.addStretch(1)
        still_row.addWidget(self.fps)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.preview, 1)
        lay.addWidget(self.status)
        lay.addLayout(head)
        lay.addLayout(still_row)

        self.btn_live.toggled.connect(self._toggle_live)
        self.cmb_size.currentIndexChanged.connect(self._on_size_changed)
        self.spn_expo.valueChanged.connect(self._on_expo_changed)
        self.spn_gain.valueChanged.connect(self._on_gain_changed)
        self.btn_snap.clicked.connect(self._snap)
        self.btn_save.clicked.connect(self._save_frame)

        # 状態表示用のタイマー (fps計測)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_fps)

    # ------------------------------------------------------------ ライブ制御
    def start_camera(self) -> None:
        """ワーカーを起動し (未起動なら)、ライブを開始する。

        ワーカーが前回エラーで死んでいる場合は作り直す (カメラの再挿入・
        占有解除からの復帰用)。
        """
        if self.worker is not None and not self.worker.isRunning():
            self.worker.wait(1000)  # 死亡済みスレッドの掃除
            self.worker = None
        if self.worker is None:
            self.worker = PreviewWorker()
            self.worker.frame_ready.connect(self._on_frame)
            self.worker.still_ready.connect(self._on_still)
            self.worker.error.connect(self._on_error)
            self.worker.state.connect(self.status.setText)
            self.worker.start()
            self._timer.start()
        self.worker.post("start", self.cmb_size.currentData())
        self.btn_live.setChecked(True)

    def stop_camera(self) -> None:
        if self.worker is not None:
            self.worker.post("stop")
        self.btn_live.setChecked(False)

    def shutdown(self) -> None:
        """パネル・アプリ終了時にカメラを解放する。"""
        self._timer.stop()
        w = self.worker
        if w is not None:
            w.quit_loop()
            # スチル待ち (最大~4s) やフル解像度撮影中は数秒かかることがある。
            # 固まった場合 (デバイス抜け等) は強制終了してプロセス終了時に
            # OS側で解放させる。
            if not w.wait(5000):
                w.terminate()
                w.wait(2000)
            self.worker = None

    def _toggle_live(self, checked: bool) -> None:
        if checked:
            self.start_camera()
        else:
            self.stop_camera()

    def _on_size_changed(self) -> None:
        if self.worker is None:
            return
        size = self.cmb_size.currentData()
        if size:
            self.worker.post("setsize", size)

    def _on_expo_changed(self) -> None:
        if self.worker is not None:
            self.worker.post("setexpo", self.spn_expo.value())

    def _on_gain_changed(self) -> None:
        if self.worker is not None:
            self.worker.post("setgain", self.spn_gain.value())

    # ------------------------------------------------------------ 表示・イベント
    def _on_frame(self, data: bytes, w: int, h: int) -> None:
        self._frames += 1
        self.preview.setPixmap(_to_pixmap(data, w, h).scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _update_fps(self) -> None:
        now = time.time()
        dt = max(now - self._fps_t0, 0.001)
        self.fps.setText(f"{self._frames / dt:.1f} fps")
        self._frames = 0
        self._fps_t0 = now

    def _on_error(self, msg: str) -> None:
        self.status.setText(f"⚠ {msg}")

    def _on_still(self, png: bytes, note: str) -> None:
        self.snapshot_ready.emit(png, note)

    # ------------------------------------------------------------ スナップ・保存
    def _snap(self) -> None:
        if self.worker is None:
            self.start_camera()
        if self.chk_full.isChecked():
            self.worker.post("still_full")
        else:
            self.worker.post("snap")

    def _save_frame(self) -> None:
        """現在表示中のフレームをPNG保存する (ライブ停止中でも最終画面)。"""
        from PySide6.QtWidgets import QFileDialog

        pm = self.preview.pixmap()
        if pm is None or pm.isNull():
            self._on_error("保存するフレームがありません (ライブを開始してください)")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "カメラ画像を保存", "camera_snap.png", "PNG (*.png);;すべてのファイル (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        pm.toImage().save(path)
        self.status.setText(f"保存しました: {path}")
