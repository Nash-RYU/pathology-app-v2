"""Swiftcam SDK (libswiftcam.so) による実撮像テスト。

ABI: ToupTek toupcam.h (SDK 57系, 接頭辞 Swiftcam_)
  unsigned Swiftcam_EnumV2(ToupcamDeviceV2 arr[128]);   # arr=NULL でカウントのみ
  HToupcam  Swiftcam_Open(const char* camId);           # NULL で1台目
  HRESULT   Swiftcam_put_Option(h, TOUPCAM_OPTION_RGB(0x0c), 0)  # RGB24
  HRESULT   Swiftcam_StartPullModeWithCallback(h, cb, ctx)
  HRESULT   Swiftcam_PullImageV2(h, void* buf, int bits, ToupcamFrameInfoV2*)
"""
import ctypes
import time

from PIL import Image

LIB = "/usr/local/SwiftImaging3.0/libswiftcam.so"
lib = ctypes.CDLL(LIB)

TOUPCAM_MAX = 128
TOUPCAM_OPTION_RGB = 0x0C       # 0 => RGB24
TOUPCAM_OPTION_BITDEPTH = 0x06  # 0 => 8 bits mode


class ToupcamDeviceV2(ctypes.Structure):
    _fields_ = [
        ("displayname", ctypes.c_char * 64),
        ("id", ctypes.c_char * 64),
        ("model", ctypes.c_void_p),
    ]

Arr = ToupcamDeviceV2 * TOUPCAM_MAX


def lib_fn(name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


enum_v2 = lib_fn("Swiftcam_EnumV2", ctypes.c_uint, ctypes.POINTER(ToupcamDeviceV2))
open_ = lib_fn("Swiftcam_Open", ctypes.c_void_p, ctypes.c_char_p)
close_ = lib_fn("Swiftcam_Close", None, ctypes.c_void_p)
get_name = lib_fn("Swiftcam_get_Name", ctypes.c_uint, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint)
get_model = lib_fn("Swiftcam_get_Model", ctypes.c_uint, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint)
get_resnum = lib_fn("Swiftcam_get_ResolutionNumber", ctypes.c_uint, ctypes.c_void_p)
get_res = lib_fn("Swiftcam_get_Resolution", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                 ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
get_size = lib_fn("Swiftcam_get_Size", ctypes.c_int, ctypes.c_void_p,
                  ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
put_size = lib_fn("Swiftcam_put_Size", ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
put_option = lib_fn("Swiftcam_put_Option", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int)
start_pull = lib_fn("Swiftcam_StartPullModeWithCallback", ctypes.c_int,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
pull_v2 = lib_fn("Swiftcam_PullImageV2", ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                 ctypes.c_int, ctypes.c_void_p)
wait_v3 = lib_fn("Swiftcam_WaitImageV3", ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                 ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
stop_ = lib_fn("Swiftcam_Stop", ctypes.c_int, ctypes.c_void_p)
get_expo = lib_fn("Swiftcam_get_ExpoTime", ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
get_gain = lib_fn("Swiftcam_get_ExpoAGain", ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort))
get_gain_range = lib_fn("Swiftcam_get_ExpoAGainRange", ctypes.c_int, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort),
                        ctypes.POINTER(ctypes.c_ushort))
get_expo_range = lib_fn("Swiftcam_get_ExpTimeRange", ctypes.c_int, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                        ctypes.POINTER(ctypes.c_uint))

# --- Enum ------------------------------------------------------------------
arr = Arr()
n = enum_v2(arr)
print("count:", n)
for i in range(n):
    d = arr[i]
    print(f"  [{i}] displayname={d.displayname.rstrip(b'\x00').decode(errors='replace')!r}")
    print(f"       id={d.id.rstrip(b'\x00').decode(errors='replace')!r} model*={d.model}")

# --- Open ------------------------------------------------------------------
h = open_(None)
if not h:
    print("open failed (NULL)")
    raise SystemExit(1)
print("handle:", hex(h))

name = ctypes.create_string_buffer(256)
get_name(h, name, 256)
model = ctypes.create_string_buffer(256)
get_model(h, model, 256)
print("name:", name.value)
print("model:", model.value)

# --- 解像度 ----------------------------------------------------------------
nres = get_resnum(h)
print("resolution number:", nres)
for i in range(nres):
    w = ctypes.c_int()
    hh = ctypes.c_int()
    get_res(h, i, ctypes.byref(w), ctypes.byref(hh))
    print(f"  [{i}] {w.value}x{hh.value}")

w = ctypes.c_int()
hh = ctypes.c_int()
get_size(h, ctypes.byref(w), ctypes.byref(hh))
print("current size:", w.value, hh.value)

# --- RGB24出力設定 + プレビュー解像度を選んで開始 ----------------------------
print("put_Option(RGB,0):", put_option(h, TOUPCAM_OPTION_RGB, 0))
print("put_Option(BITDEPTH,0):", put_option(h, TOUPCAM_OPTION_BITDEPTH, 0))
# プレビュー用に小さい解像度を選ぶ (pullモードはプレビュー解像度を要求)
w = ctypes.c_int()
hh = ctypes.c_int()
get_res(h, nres - 1, ctypes.byref(w), ctypes.byref(hh))
print("put_Size:", put_size(h, w.value, hh.value), "->", w.value, hh.value)
print("start_pull:", start_pull(h, None, None))

et = ctypes.c_uint()
get_expo(h, ctypes.byref(et))
g = ctypes.c_ushort()
get_gain(h, ctypes.byref(g))
print("expo:", et.value, "us, gain:", g.value)

WIDTH, HEIGHT = w.value, hh.value
print("pull size:", WIDTH, HEIGHT)

buf = ctypes.create_string_buffer(WIDTH * HEIGHT * 3)
info = ctypes.create_string_buffer(64)  # ToupcamFrameInfoV2 (24バイト以上)

t0 = time.time()
frames = 0
for k in range(30):
    ret = wait_v3(h, 2000, buf, 0, 24, 0, info)
    if ret < 0:
        print("wait ret:", hex(ret & 0xFFFFFFFF))
        break
    frames += 1
print(f"frames={frames} in {time.time() - t0:.2f}s  ({frames / max(time.time() - t0, 0.01):.1f} fps)")

if frames > 0:
    img = Image.frombytes("RGB", (WIDTH, HEIGHT), buf.raw[:WIDTH * HEIGHT * 3])
    img.save("/home/yuunishi/pathology-app-v2/tools/swiftcam_frame.png")
    print("saved swiftcam_frame.png")

stop_(h)
close_(h)
print("closed")
