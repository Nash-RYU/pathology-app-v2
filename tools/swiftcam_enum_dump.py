"""Swiftcam_Enum の戻り値を生ダンプしてデバイスレコード構造を確定させる。"""
import ctypes

LIB = "/usr/local/SwiftImaging3.0/libswiftcam.so"
lib = ctypes.CDLL(LIB)

lib.Swiftcam_EnumV2.restype = ctypes.c_int
lib.Swiftcam_EnumV2.argtypes = [ctypes.c_int, ctypes.c_int]
lib.Swiftcam_Enum.restype = ctypes.c_void_p
lib.Swiftcam_Enum.argtypes = [ctypes.c_int, ctypes.c_int]

count = lib.Swiftcam_EnumV2(0, 0)
print("EnumV2 count:", count)
ptr = lib.Swiftcam_Enum(0, 0)
print("Enum ptr:", hex(ptr or 0))
if not ptr:
    raise SystemExit("null")

# count * 0x238 + 余裕 をダンプ
total = count * 0x238 + 0x200
raw = ctypes.string_at(ptr, total)
print(f"dump {len(raw)} bytes")

def show(off, n, label):
    seg = raw[off:off + n]
    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in seg)
    print(f"  {off:4d} {label:20s}: {txt}")

for i in range(count):
    base = i * 0x238
    print(f"--- device[{i}] @ {base:#x} ---")
    show(base + 0, 64, "name?")
    show(base + 64, 64, "camera?")
    for off in (128, 136, 144, 160, 176, 192, 224, 256, 288, 320, 352, 384, 416, 448, 480, 512):
        seg = raw[base + off:base + off + 8]
        u64 = struct.unpack("<Q", seg)[0]
        seg32 = struct.unpack("<2I", seg)
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in seg)
        print(f"  {off:4d}: u64={u64:#018x} u32={seg32[0]:#010x},{seg32[1]:#010x} |{txt}|")
    show(base + 128, 128, "tail text?")

import struct
