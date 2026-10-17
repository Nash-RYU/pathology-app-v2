"""Swiftcam SC1003 (0547:a39b) 実機プローブ。

tucsen-tca-camera (0547:c003) で逆解析されたプロトコルが本機に適用できるか
段階的に確認する:
  1. EP0 vendor コマンド (0xb4/0xb5/0xb7) が「10バイトIN + EPIPEストール」の
     特徴的な署名を返すか
  2. bulk EP からのレコードが 0x88 マーカー始まりか
"""
import sys
import usb.core
import usb.util

VID, PID = 0x0547, 0xA39B
BULK_EP = 0x81  # tucsen c003 では 0x82
RECORD = 1229312  # mode 2 (1280x960) のレコードサイズ

dev = usb.core.find(idVendor=VID, idProduct=PID)
if dev is None:
    print("NO DEVICE")
    sys.exit(1)
print(f"device: {dev.manufacturer!r} {dev.product!r} bus={dev.bus} addr={dev.address}")
print("kernel driver attached:", getattr(dev, "is_kernel_driver", lambda i: False)(0))

try:
    dev.detach_kernel_driver(0)
except Exception as e:
    print("detach:", e)
usb.util.claim_interface(dev, 0)
print("claimed ok")


def vendor_cmd(breq, wvalue, windex, nread=10):
    """EP0 vendorコマンドを送り、IN data stage の結果を返す。"""
    try:
        data = dev.ctrl_transfer(
            0xC0, breq, wvalue, windex, nread, timeout=1000
        )
        return ("OK", bytes(data))
    except usb.core.USBError as e:
        return ("ERR", e.errno, str(e))


SEQ = [
    (0xB4, 0x00C2, 0x0000, 500),   # mode 2 選択 (c003での値)
    (0xB5, 0x00A2, 0x0000, 30),
    (0xB7, 0x1054, 0x305E, 30),
    (0xB7, 0x0064, 0x3012, 30),
    (0xB5, 0x00A1, 0x0000, 30),
    (0xB7, 0x1054, 0x305E, 30),
    (0xB7, 0x0D0F, 0x3012, 120),
]

import time
for breq, wvalue, windex, wait_ms in SEQ:
    r = vendor_cmd(breq, wvalue, windex)
    print(f"cmd {breq:#04x} w={wvalue:#06x} i={windex:#06x} -> {r}")
    time.sleep(wait_ms / 1000)

# ---- bulk read 試行 -------------------------------------------------
try:
    data = dev.read(BULK_EP, RECORD, timeout=5000)
    buf = bytes(data)
    print(f"bulk read: {len(buf)} bytes, head={buf[:16].hex()}")
except usb.core.USBError as e:
    print("bulk read failed:", e)
    buf = None

if buf and len(buf) > 64:
    head = buf[:512]
    nz = sum(1 for b in head if b)
    print(f"head512: non-zero={nz}, markers88={head.count(0x88)}")
    # 0x88マーカーを探す
    for off in (0, 1, 2, 8, 10, 512, 1024):
        seg = buf[off:off + 10]
        print(f"  off {off}: {seg.hex()}")
usb.util.dispose_resources(dev)
