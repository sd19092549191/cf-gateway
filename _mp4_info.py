"""读取 MP4 宽高与时长（纯 Python 解析 box，不依赖 ffprobe）。

用法: python _mp4_info.py file1.mp4 [file2.mp4 ...]
  tkhd 宽高按 version 0/1 分别取偏移；mvhd 时长按 version 0/1 解析。
"""
import struct
import sys


def _boxes(buf, start, end):
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", buf[i:i + 4])[0]
        typ = buf[i + 4:i + 8]
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr:
            break
        yield typ, i + hdr, i + size
        i += size


def _find(buf, path, start, end):
    cur = [(start, end)]
    for name in path:
        nxt = []
        for s, e in cur:
            for typ, bs, be in _boxes(buf, s, e):
                if typ == name:
                    nxt.append((bs, be))
        cur = nxt
    return cur


def mp4_info(buf):
    dur = None
    for s, e in _find(buf, [b"moov"], 0, len(buf)):
        for typ, bs, _ in _boxes(buf, s, e):
            if typ == b"mvhd":
                if buf[bs] == 0:
                    ts, du = struct.unpack(">II", buf[bs + 12:bs + 20])
                else:
                    ts, du = struct.unpack(">IQ", buf[bs + 20:bs + 28])
                dur = du / ts
    wh = None
    for s, e in _find(buf, [b"moov", b"trak"], 0, len(buf)):
        for typ, bs, _ in _boxes(buf, s, e):
            if typ == b"tkhd":
                off = bs + (4 + 20 + 16 + 36 if buf[bs] == 0 else 4 + 32 + 16 + 36)
                w, h = struct.unpack(">II", buf[off:off + 8])
                w >>= 16
                h >>= 16
                if w and h:
                    wh = (w, h)
                    break
        if wh:
            break
    return wh, dur


def main():
    for p in sys.argv[1:]:
        buf = open(p, "rb").read()
        wh, dur = mp4_info(buf)
        size = len(buf) / 1024 / 1024
        res = f"{wh[0]}x{wh[1]}" if wh else "未解析"
        du = f"{dur:.2f}s" if dur else "未解析"
        print(f"{p} | {res} | {du} | {size:.2f} MB")


if __name__ == "__main__":
    main()
