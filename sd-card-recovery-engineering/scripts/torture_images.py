#!/usr/bin/env python3
"""Write damaged and edge-case disk images that a photo-recovery tool must survive.

Usage:  python3 torture_images.py OUTDIR [--big]

Each image is listed with the behaviour a robust tool should show. Run the
tool's recover command on every file: none may print a Python traceback.
--big adds a 300 MiB TIFF case (writes 300 MiB of real data).
Standard library only; nothing here touches a real device.
"""
import struct
import sys
from pathlib import Path

SECTOR = 512


def jpeg(size=20000):
    """A structurally valid JPEG (SOI, APP0, SOS, scan data, EOI). Not decodable, but
    any segment-walking length finder must measure it exactly."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
    scan = bytes((i % 251) + 1 for i in range(size))      # never 0xFF, so no stray markers
    return b"\xff\xd8" + app0 + sos + scan + b"\xff\xd9"


def pad(data):
    return data + b"\x00" * (-len(data) % SECTOR)


def protective_mbr():
    mbr = bytearray(SECTOR)
    mbr[446 + 4] = 0xEE                                    # type: GPT follows
    mbr[446 + 8:446 + 12] = struct.pack("<I", 1)
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr)


def raf_header(total):
    h = bytearray(b"FUJIFILMCCD-RAW 0201FF383501" + b"\x00" * 84)
    struct.pack_into(">6I", h, 84, 0x1000, 0x1000, 0, 0, 0x10000, total - 0x10000)
    return bytes(h)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    cases = []

    def write(name, data=b"", size=None, expect=""):
        p = out / name
        with open(p, "wb") as f:
            f.write(data)
            if size:
                f.truncate(size)                           # sparse: costs no real disk space
        cases.append((name, expect))

    write("gpt_truncated.img", protective_mbr() + b"EFI PART" + b"\x00" * 70,
          expect="no crash; 'no readable volume - carving only'")

    hdr = bytearray(92)
    hdr[:8] = b"EFI PART"
    struct.pack_into("<QII", hdr, 72, 2, 0xFFFFFFFF, 0xFFFFFFFF)
    write("gpt_absurd.img", protective_mbr() + bytes(hdr) + b"\x00" * 4096,
          expect="no crash, no long loop; carving only")

    cr3 = struct.pack(">I4s4sI", 24, b"ftyp", b"crx ", 1) + b"\x00" * 8
    cr3 += struct.pack(">I4s", 5000, b"uuid") + b"\x00" * (5000 - 8)
    cr3 += struct.pack(">I4sQ", 1, b"mdat", 1 << 40)      # box claims 1 TiB
    write("cr3_overshoot.img", b"\x00" * 65536 + pad(cr3) + pad(jpeg()) + b"\x00" * 65536,
          expect="CR3: Cannot recover (runs past the end); the JPEG after it: Recovered")

    write("raf_over_cap.img", raf_header(300 << 20), size=400 << 20,
          expect="Cannot recover: larger than the size limit (not silently skipped)")

    write("raf_past_end.img", raf_header(150 << 20), size=100 << 20,
          expect="Cannot recover: runs past the end of the card")

    write("jpeg_cut_off.img", b"\x00" * 65536 + jpeg()[:15000],
          expect="nothing carved, no crash")

    write("empty.img", expect="one-line error: the image is empty")

    if "--big" in sys.argv:
        p = out / "tiff_over_cap.img"
        with open(p, "wb") as f:
            f.write(b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 5) + b"\x01" * 502)
            block = (bytes(range(1, 256)) * 4112)[:1 << 20]  # nonzero, no headers at sector starts
            for _ in range(300):
                f.write(block)
        cases.append((p.name, "Failed to recover: saved but cut at the size limit"))

    for name, expect in cases:
        print(f"{name:<20} {expect}")


if __name__ == "__main__":
    main()
