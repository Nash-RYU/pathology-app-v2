"""静的バッファ (0x1623520) を直接読んでデバイスレコードを解析する。"""
import ctypes
import re

LIB = "/usr/local/SwiftImaging3.0/libswiftcam.so"
lib = ctypes.CDLL(LIB)
lib.Swiftcam_EnumV2.restype = ctypes.c_int
lib.Swiftcam_EnumV2.argtypes = [ctypes.c_int, ctypes.c_int]

count = lib.Swiftcam_EnumV2(0, 0)
print("count:", count)

# 自分のプロセス内でのロードアドレスを取得
base = None
with open("/proc/self/maps") as f:
    for line in f:
        if line.rstrip().endswith("libswiftcam.so"):
            m = re.match(r"([0-9a-f]+)-", line)
            base = int(m.group(1), 16)
            break
if base is None:
    raise SystemExit("libswiftcam not mapped")

STATIC = base + 0x1623520
print(f"lib base={base:#x}, static={STATIC:#x}")
raw = ctypes.string_at(STATIC, count * 0x238 + 0x1A0)
print(f"dump {len(raw)} bytes")

def txt(seg):
    return "".join(chr(b) if 32 <= b < 127 else "." for b in seg)

for i in range(count):
    b = i * 0x238
    print(f"--- device[{i}] @ {b:#x} ---")
    print("  name  :", repr(txt(raw[b:b + 64]).strip(".")))
    print("  camera:", repr(txt(raw[b + 64:b + 128]).strip(".")))
    for off in range(128, 0x238, 16):
        seg = raw[b + off:b + off + 16]
        u32 = struct.unpack("<4I", seg)
        print(f"  {off:4d}: {seg.hex()}  u32={','.join(hex(v) for v in u32)} |{txt(seg)}|")
