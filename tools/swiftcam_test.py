"""libswiftcam.so を ctypes で叩く実験 (ToupTek系SDK ABI)。"""
import ctypes
import struct
import sys

LIB = "/usr/local/SwiftImaging3.0/libswiftcam.so"
lib = ctypes.CDLL(LIB)

# --- Version --------------------------------------------------------
lib.Swiftcam_Version.restype = ctypes.c_char_p
try:
    print("Version:", lib.Swiftcam_Version())
except Exception as e:
    print("Version err:", e)

# --- EnumV2 ---------------------------------------------------------
lib.Swiftcam_EnumV2.restype = ctypes.c_void_p
lib.Swiftcam_EnumV2.argtypes = [ctypes.c_int, ctypes.c_int]
list_ptr = lib.Swiftcam_EnumV2(0, 0)
print("enum ptr:", list_ptr)
if not list_ptr:
    print("NO LIST")
    sys.exit(0)

# ToupcamDevice (推定):
#   name[16], camera[16], major, minor, speed, lMaxSpeed, lReserved,
#   flags, production[8], lens[16], mmgr[16], hw[16]
# まずは生ダンプで構造を確認
raw = ctypes.string_at(list_ptr, 512)
for off in range(0, 256, 16):
    seg = raw[off:off + 16]
    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in seg)
    hx = seg.hex()
    print(f"  {off:3d} (0x{off:02x}): {hx}  {txt}")
