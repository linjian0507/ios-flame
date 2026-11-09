#!/usr/bin/env python3
"""Reconstruct PNG screenshots from SWEET FB MAP serial rows.

Rows are 48x106 RGB332 hex dumps emitted by SweetFramebuffer.cpp. The
result is a coarse panel-memory capture, not a physical-panel photo.
"""
import argparse
import re
import struct
import zlib
from pathlib import Path

LINE = re.compile(r"SWEET FB MAP (\d+) (\d+) ([0-9a-f]+)", re.I)
HDLINE = re.compile(r"SWEET FB HDMAP (\d+) ([0-9a-f]{384})", re.I)
WIDTH, HEIGHT = 48, 106
HDW, HDH = 56, 120


def parse(text):
    maps = {}
    for m in LINE.finditer(text):
        slot, row, data = int(m[1]), int(m[2]), m[3].lower()
        if row >= HEIGHT or len(data) != WIDTH * 2:
            continue
        maps.setdefault(slot, {})[row] = bytes.fromhex(data)
    return maps


def parse_hd(text):
    rows = {}
    for line in text.splitlines():
        m = re.search(r"SWEET FB HDMAP (\d+)", line, re.I)
        if not m:
            continue
        row = int(m[1])
        # Console writers can interleave another log line inside ours.
        # Our hex payload is the only run of long lowercase-hex chunks,
        # so concatenate those and ignore the interleaved text.
        runs = re.findall(r"[0-9a-f]{32,}", line[m.end():], re.I)
        parts = []
        for run in runs:
            if len(run) % 4:
                run = run[len(run) % 4:]
            parts.append(run)
        data = "".join(parts)[:HDW * 4]
        if row >= HDH or len(data) != HDW * 4:
            continue
        rows[row] = bytes.fromhex(data)
    return rows


def rgb565(v):
    v = int.from_bytes(v, "big")
    r = (v >> 11) & 31
    g = (v >> 5) & 63
    b = v & 31
    return (r * 255 // 31, g * 255 // 63, b * 255 // 31)


def rgb332(v):
    r = (v >> 5) & 7
    g = (v >> 2) & 7
    b = v & 3
    return (r * 255 // 7, g * 255 // 7, b * 255 // 3)


def png(width, height, rows):
    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body +
                struct.pack(">I", zlib.crc32(body) & 0xffffffff))
    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) +
            chunk(b"IEND", b""))


def write_png(maps, slot, path, scale=10):
    rows = []
    for gy in range(HEIGHT):
        data = maps.get(slot, {}).get(gy)
        if data is None:
            data = bytes(WIDTH)
        line = bytearray()
        for v in data:
            r, g, b = rgb332(v)
            line.extend(bytes((r, g, b)) * scale)
        rows.extend([bytes(line)] * scale)
    path.write_bytes(png(WIDTH * scale, HEIGHT * scale, rows))


def write_hd_png(rows, path, scale=5):
    pixels = []
    for gy in range(HDH):
        data = rows.get(gy, bytes(HDW * 2))
        line = bytearray()
        for i in range(HDW):
            r, g, b = rgb565(data[i * 2:i * 2 + 2])
            line.extend(bytes((r, g, b)) * scale)
        pixels.extend([bytes(line)] * scale)
    path.write_bytes(png(HDW * scale, HDH * scale, pixels))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--outdir", type=Path, default=Path("work/fb-maps"))
    args = parser.parse_args()
    text = args.capture.read_text(encoding="utf-8", errors="replace")
    maps = parse(text)
    hd = parse_hd(text)
    if not maps and not hd:
        print("no SWEET FB MAP rows found")
        return 1
    args.outdir.mkdir(parents=True, exist_ok=True)
    for slot in sorted(maps):
        rows = maps[slot]
        pixels = [v for row in rows.values() for v in row]
        nonblack = sum(1 for v in pixels if v)
        path = args.outdir / f"panel-slot{slot}.png"
        write_png(maps, slot, path)
        print(f"slot {slot}: rows {len(rows)}/{HEIGHT} nonblack {nonblack}/{len(pixels)} -> {path}")
    if hd:
        path = args.outdir / "panel-hd.png"
        write_hd_png(hd, path)
        print(f"HD: rows {len(hd)}/{HDH} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
