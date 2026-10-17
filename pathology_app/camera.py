"""Swiftcam (ToupTek系) SDKラッパー — カメラ認識機能の中核。

本機のカメラは Swiftcam SC1003 (USB VID:PID 0547:a39b, ToupTek製USB3,
vendor固有クラス) で、Linux標準のUVCドライバでは /dev/video* が作られない。
そこで Swift Imaging 3.0 同梱の公式SDK (/usr/local/SwiftImaging3.0/libswiftcam.so,
ToupTek toupcam.h と同一ABI, 接頭辞 Swiftcam_) を ctypes で直接叩く。

検証済みの呼び出しシーケンス (実機 SC1003 で確認):
  n = Swiftcam_EnumV2(ToupcamDeviceV2 arr[128])   # NULL渡しでカウントのみ
  h = Swiftcam_Open(NULL or cam_id)               # NULLで1台目
  Swiftcam_put_Option(h, 0x0C (RGB), 0)           # RGB24出力 (戻り1=OK)
  Swiftcam_put_Size(h, w, h)                      # 解像度は get_Resolution 一覧から
  Swiftcam_StartPullModeWithCallback(h, NULL, NULL)
  Swiftcam_WaitImageV3(h, ms, buf, bStill, 24, 0, info)   # buf=w*h*3
  Swiftcam_Snap(h, 0xffffffff) → WaitImageV3(bStill=1)    # スチルは
      「現在のプレビュー解像度」のRGB24で即座に届く (実測: 912x686)
  フル解像度スチルは put_Size(3664,2748) してpullで1枚取る (~3fps)

すべてのデバイス操作 (Open/put_Size/Snap/WaitImageV3) は1つのスレッドから
行うこと (SDKの規約)。このモジュールでは SwiftcamCamera を worker スレッド
1本に封入し、GUI からはコマンドキュー経由で指示する設計にしている
(camera_panel.py の PreviewWorker 参照)。
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from dataclasses import dataclass

import config

# ---------------------------------------------------------------- 既定値
DEFAULT_LIB = "/usr/local/SwiftImaging3.0/libswiftcam.so"
ENV_LIB = "PATHOLOGY_SWIFTCAM_LIB"

TOUPCAM_MAX = 128
TOUPCAM_OPTION_RGB = 0x0C       # value=0 → RGB24 (8bit)
TOUPCAM_OPTION_BITDEPTH = 0x06  # value=0 → 8bit モード (実機は実行中に変えられない)

# 実機で確認した解像度一覧 (3664x2748 / 1832x1374 / 912x686)
PREVIEW_SIZES = ((912, 686), (1832, 1374), (3664, 2748))
STILL_TIMEOUT_MS = 800  # スチル (Snap) の待ち。取りこぼし時はpullにフォールバック

_HRESULT_OK = 0


class CameraError(Exception):
    """カメラ操作の失敗 (未導入・占用・撮像失敗など)。"""


# ---------------------------------------------------------------- ctypes
class ToupcamDeviceV2(ctypes.Structure):
    _fields_ = [
        ("displayname", ctypes.c_char * 64),
        ("id", ctypes.c_char * 64),
        ("model", ctypes.c_void_p),
    ]


_Arr = ToupcamDeviceV2 * TOUPCAM_MAX

_LIB = None


def _load_lib() -> ctypes.CDLL:
    """libswiftcam.so をロードし、Swiftcam_* 関数のシグネチャを設定する。"""
    global _LIB
    if _LIB is not None:
        return _LIB
    path = os.environ.get(ENV_LIB, getattr(config, "SWIFTCAM_LIB", DEFAULT_LIB))
    try:
        lib = ctypes.CDLL(path)
    except OSError as e:
        raise CameraError(
            f"Swiftcam SDK ({path}) をロードできません: {e}"
        ) from e

    def fn(name, restype, *argtypes):
        f = getattr(lib, name)
        f.restype = restype
        f.argtypes = list(argtypes)
        return f

    fn("Swiftcam_Version", ctypes.c_char_p)
    fn("Swiftcam_EnumV2", ctypes.c_uint, ctypes.POINTER(ToupcamDeviceV2))
    fn("Swiftcam_Open", ctypes.c_void_p, ctypes.c_char_p)
    fn("Swiftcam_Close", None, ctypes.c_void_p)
    fn("Swiftcam_get_Name", ctypes.c_uint, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint)
    fn("Swiftcam_get_ResolutionNumber", ctypes.c_uint, ctypes.c_void_p)
    fn("Swiftcam_get_Resolution", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
       ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    fn("Swiftcam_get_Size", ctypes.c_int, ctypes.c_void_p,
       ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    fn("Swiftcam_put_Size", ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
    fn("Swiftcam_put_Option", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int)
    fn("Swiftcam_StartPullModeWithCallback", ctypes.c_int,
       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    fn("Swiftcam_WaitImageV3", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
       ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    fn("Swiftcam_Snap", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    fn("Swiftcam_Stop", ctypes.c_int, ctypes.c_void_p)
    fn("Swiftcam_get_ExpoTime", ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
    fn("Swiftcam_put_ExpoTime", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    fn("Swiftcam_get_ExpTimeRange", ctypes.c_int, ctypes.c_void_p,
       ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
       ctypes.POINTER(ctypes.c_uint))
    fn("Swiftcam_get_ExpoAGain", ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort))
    fn("Swiftcam_put_ExpoAGain", ctypes.c_int, ctypes.c_void_p, ctypes.c_ushort)
    fn("Swiftcam_get_ExpoAGainRange", ctypes.c_int, ctypes.c_void_p,
       ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort),
       ctypes.POINTER(ctypes.c_ushort))
    _LIB = lib
    return lib


# ---------------------------------------------------------------- 列挙
@dataclass
class CameraInfo:
    displayname: str
    cam_id: str


def list_cameras() -> list[CameraInfo]:
    """接続中のSwiftcamカメラ一覧を返す (SDK未導入なら CameraError)。"""
    lib = _load_lib()
    arr = _Arr()
    n = lib.Swiftcam_EnumV2(arr)
    out = []
    for i in range(n):
        dev = arr[i]
        out.append(CameraInfo(
            displayname=dev.displayname.decode("utf-8", "replace").strip("\x00"),
            cam_id=dev.id.decode("utf-8", "replace").strip("\x00"),
        ))
    return out


# ---------------------------------------------------------------- 制御
class SwiftcamCamera:
    """Swiftcam SDK 1台分のラッパー。

    スレッド規約: このインスタンスの open / start / stop / wait_frame /
    snap_still / set_* はすべて **1つのスレッドから** 呼ぶこと。
    (camera_panel.py の PreviewWorker がその役割を担う)
    """

    def __init__(self, cam_id: str | None = None):
        self._lib = _load_lib()
        self.cam_id = cam_id          # None → 1台目
        self.displayname = ""
        self._h: int | None = None
        self._running = False
        self._w = 0
        self._hh = 0
        self._buf = None              # pull用バッファ (サイズ変更時に作り直す)
        self._buf_size: tuple[int, int] | None = None

    # -------------------------------------------------------------- open/close
    def open(self) -> None:
        h = self._lib.Swiftcam_Open(
            None if self.cam_id is None else self.cam_id.encode("utf-8")
        )
        if not h:
            raise CameraError(
                "カメラを開けませんでした (Swift Imaging 3.0 など別のアプリが"
                "カメラを占有している場合は閉じてください)"
            )
        self._h = h
        arr = _Arr()
        n = self._lib.Swiftcam_EnumV2(arr)
        for i in range(n):
            dev = arr[i]
            cid = dev.id.decode("utf-8", "replace").strip("\x00")
            if self.cam_id is None or cid == self.cam_id:
                self.displayname = (
                    dev.displayname.decode("utf-8", "replace").strip("\x00")
                    or cid or "Swiftcam"
                )
                break
        else:
            self.displayname = self.cam_id or "Swiftcam"
        # RGB24出力 (BITDEPTHはカメラ実行中に変更不可なのでここでは試さない)
        self._lib.Swiftcam_put_Option(h, TOUPCAM_OPTION_RGB, 0)

    def close(self) -> None:
        if self._running:
            self.stop()
        if self._h:
            self._lib.Swiftcam_Close(self._h)
            self._h = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -------------------------------------------------------------- 解像度
    def resolutions(self) -> list[tuple[int, int]]:
        """カメラが提供する解像度一覧 (降順 = 高解像度が先頭)。"""
        if not self._h:
            raise CameraError("カメラが開かれていません")
        n = self._lib.Swiftcam_get_ResolutionNumber(self._h)
        out = []
        w, hh = ctypes.c_int(), ctypes.c_int()
        for i in range(n):
            self._lib.Swiftcam_get_Resolution(
                self._h, i, ctypes.byref(w), ctypes.byref(hh)
            )
            if w.value > 0 and hh.value > 0:
                out.append((w.value, hh.value))
        out.sort(reverse=True)
        return out

    def set_size(self, w: int, hh: int) -> None:
        """プレビュー解像度を設定する (start前またはstop後に呼ぶ)。

        実行中に解像度を変える場合は stop() → set_size() → start() の順で。
        """
        if not self._h:
            raise CameraError("カメラが開かれていません")
        ret = self._lib.Swiftcam_put_Size(self._h, int(w), int(hh))
        if ret < 0:
            raise CameraError(f"解像度の設定に失敗 ({w}×{hh}, ret={ret:#x})")
        self._w, self._hh = int(w), int(hh)

    def size(self) -> tuple[int, int]:
        if not self._h:
            raise CameraError("カメラが開かれていません")
        w, hh = ctypes.c_int(), ctypes.c_int()
        self._lib.Swiftcam_get_Size(self._h, ctypes.byref(w), ctypes.byref(hh))
        return w.value, hh.value

    # -------------------------------------------------------------- 露出・ゲイン
    def exposure_range(self) -> tuple[int, int]:
        if not self._h:
            raise CameraError("カメラが開かれていません")
        lo, hi, step = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
        self._lib.Swiftcam_get_ExpTimeRange(
            self._h, ctypes.byref(lo), ctypes.byref(hi), ctypes.byref(step)
        )
        return lo.value, hi.value

    def get_exposure(self) -> int:
        if not self._h:
            raise CameraError("カメラが開かれていません")
        v = ctypes.c_uint()
        self._lib.Swiftcam_get_ExpoTime(self._h, ctypes.byref(v))
        return v.value

    def set_exposure(self, us: int) -> None:
        if not self._h:
            raise CameraError("カメラが開かれていません")
        self._lib.Swiftcam_put_ExpoTime(self._h, int(us))

    def gain_range(self) -> tuple[int, int]:
        if not self._h:
            raise CameraError("カメラが開かれていません")
        lo, hi, step = ctypes.c_ushort(), ctypes.c_ushort(), ctypes.c_ushort()
        self._lib.Swiftcam_get_ExpoAGainRange(
            self._h, ctypes.byref(lo), ctypes.byref(hi), ctypes.byref(step)
        )
        return lo.value, hi.value

    def get_gain(self) -> int:
        if not self._h:
            raise CameraError("カメラが開かれていません")
        v = ctypes.c_ushort()
        self._lib.Swiftcam_get_ExpoAGain(self._h, ctypes.byref(v))
        return v.value

    def set_gain(self, gain: int) -> None:
        if not self._h:
            raise CameraError("カメラが開かれていません")
        self._lib.Swiftcam_put_ExpoAGain(self._h, int(gain))

    # -------------------------------------------------------------- pull
    def start(self) -> None:
        """pullモードを開始する (コールバックなし)。"""
        if not self._h:
            raise CameraError("カメラが開かれていません")
        if self._running:
            return
        ret = self._lib.Swiftcam_StartPullModeWithCallback(self._h, None, None)
        if ret < 0:
            raise CameraError(f"pullモードの開始に失敗 (ret={ret:#x})")
        self._running = True

    def stop(self) -> None:
        if not self._h:
            return
        if self._running:
            self._lib.Swiftcam_Stop(self._h)
            self._running = False

    def wait_frame(self, timeout_ms: int = 2000) -> tuple[bytes, int, int] | None:
        """プレビューフレーム1枚を (RGB24 bytes, w, h) で返す。

        タイムアウト・エラー時は None (呼び出し側で継続判断する)。
        バッファは解像度が変わらない限り使い回す (毎回確保すると
        実機で23fps→3fpsにまで落ちるため)。
        """
        if not self._h or not self._running:
            return None
        w, hh = self._w, self._hh
        if w <= 0 or hh <= 0:
            w, hh = self.size()
            self._w, self._hh = w, hh
        if w <= 0 or hh <= 0:
            return None
        if self._buf is None or self._buf_size != (w, hh):
            self._buf = ctypes.create_string_buffer(w * hh * 3)
            self._buf_size = (w, hh)
        buf = self._buf
        info = ctypes.create_string_buffer(64)
        ret = self._lib.Swiftcam_WaitImageV3(
            self._h, int(timeout_ms), buf, 0, 24, 0, info
        )
        if ret < 0:
            return None
        uw = ctypes.cast(info, ctypes.POINTER(ctypes.c_uint * 2)).contents[0]
        uh = ctypes.cast(info, ctypes.POINTER(ctypes.c_uint * 2)).contents[1]
        if uw > 0 and uh > 0:
            if int(uw) != w or int(uh) != hh:
                # 実フレームがバッファ確保サイズと合わない場合 (はみ出し読み
                # を防ぐためこの1枚は捨て、次回に合わせて再確保する)
                self._w, self._hh = int(uw), int(uh)
                return None
        return bytes(buf.raw[: w * hh * 3]), w, hh

    # -------------------------------------------------------------- スチル
    def snap_still(self, timeout_ms: int = STILL_TIMEOUT_MS) -> tuple[bytes, int, int]:
        """スチル撮像 (Snap)。現在のプレビュー解像度のRGB24で返る。

        実機では明るい視野 (fpsが高い) のときに Snap で要求したスチルが
        取りこぼされることがある (待っても届かない)。その場合は
        連続撮像 (pull) の次のフレームにフォールバックする — Snapの
        スチルもプレビューも同じ解像度・露出なので品質は同等。
        """
        if not self._h:
            raise CameraError("カメラが開かれていません")
        w, hh = (self._w or self.size()[0], self._hh or self.size()[1])
        if w <= 0 or hh <= 0:
            raise CameraError("解像度が不明です")
        if not self._running:
            self.start()
        buf = ctypes.create_string_buffer(w * hh * 3)
        info = ctypes.create_string_buffer(64)
        self._lib.Swiftcam_Snap(self._h, 0xFFFFFFFF)
        ret = self._lib.Swiftcam_WaitImageV3(
            self._h, int(timeout_ms), buf, 1, 24, 0, info
        )
        if ret >= 0:
            return bytes(buf.raw[: w * hh * 3]), w, hh
        # フォールバック: 連続撮像の次のフレーム (Snapと同じ解像度)。
        # 暗い視野では1フレームが長いことがあるため余裕を持たせる。
        return self.wait_frame(3000) or (b"", 0, 0)

    def full_res_still(self, timeout_ms: int = 8000) -> tuple[bytes, int, int]:
        """フル解像度スチル (プレビュー解像度と独立)。

        実機では Snap スチルはプレビュー解像度で届くため、フル解像度が
        必要なときは解像度を一時的に最高解像度へ切り替えてpullで撮る。
        戻り値: (RGB24 bytes, w, h)。プレビュー設定は元に戻す。
        """
        if not self._h:
            raise CameraError("カメラが開かれていません")
        was_running = self._running
        if was_running:
            self.stop()
        try:
            w, hh = self.size()
            resolutions = self.resolutions()
            if resolutions:
                fw, fh = resolutions[0]
            else:
                fw, fh = w, hh
            self.set_size(fw, fh)
            self.start()
            data: tuple[bytes, int, int] | None = None
            # 最初の1枚は露出切替直後の不安定フレームのことがあるため捨てる
            for k in range(3):
                got = self.wait_frame(int(timeout_ms))
                if got:
                    data = got
                    if k >= 1:
                        break
            return data or (b"", 0, 0)
        finally:
            self.stop()
            try:
                if self._w and self._hh:
                    self.set_size(self._w, self._hh)
            except CameraError:
                pass
            if was_running:
                self.start()

    # -------------------------------------------------------------- 補助
    @staticmethod
    def png_from_frame(data: bytes, w: int, hh: int) -> bytes | None:
        """RGB24フレームをPNGバイト列へ変換する (長辺1280pxに縮小)。"""
        try:
            import io

            from PIL import Image
            img = Image.frombytes("RGB", (w, hh), data)
            m = max(w, hh)
            if m > 1280:
                r = 1280 / m
                img = img.resize(
                    (max(1, round(w * r)), max(1, round(hh * r))),
                    Image.Resampling.LANCZOS if hasattr(Image, "Resampling")
                    else Image.LANCZOS,
                )
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return bytes(buf.getvalue())
        except Exception:
            return None
