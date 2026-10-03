#!/usr/bin/env python3
"""
sd_recover.py - recover deleted photos (JPEG / PNG / common RAW) from an SD card.

Phase 1  Filesystem-aware: parses FAT16 / FAT32 / exFAT (MBR or GPT), finds deleted
         directory entries and restores original names and folders. Assumes the
         file was stored contiguously (true for most camera-written files).
Phase 2  Signature carving: scans free space for photo headers and cuts files out
         by parsing their structure. Catches what Phase 1 can't (e.g. after a
         quick format). Duplicates of Phase 1 results are skipped by SHA-256.

SAFETY: the card is only ever read, never written, and your Mac's own disks are
never read. On macOS just run it: it lists the cards it can see, asks which one
to scan, copies the whole card to an image when there is room (otherwise reads
the card directly) and recovers from that:

    sudo python3 sd_recover.py                              # asks for the card's name
    sudo python3 sd_recover.py scan --volume Untitled -o ~/recovered

Manual mode, any OS:

    python3 sd_recover.py image /dev/sdX card.img          # Linux (use sudo)
    python3 sd_recover.py image /dev/rdiskN card.img       # macOS (use sudo)
    python3 sd_recover.py image \\\\.\\PhysicalDriveN card.img   # Windows (admin)
    python3 sd_recover.py recover card.img -o recovered/

Output: filesystem/ and carved/ hold the recovered files, suspect/ the ones that
failed --verify. report.csv has one row per file with details; summary.csv lists
the files in three columns: Recovered successfully / Failed to recover / Cannot recover.

Stop using the card as soon as you notice files are missing: every new photo
you take can overwrite the data you want back. Unmount the card before you scan
it (macOS: diskutil unmountDisk /dev/diskN); the program can still read it.

Pure standard library. Pillow (optional) enables --verify.
"""
import argparse
import csv
import errno
import hashlib
import os
import plistlib
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path

SECTOR = 512
PNG_SIG = b"\x89PNG\r\n\x1a\n"
MAX_JPEG = MAX_PNG = 64 << 20
MAX_BMFF = MAX_RAW = 256 << 20         # uncompressed 100 MP RAW files approach 200 MiB
CHUNK = 8 << 20

OK, FAILED, CANNOT = "Recovered successfully", "Failed to recover", "Cannot recover"
DISK_FULL = (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".cr2", ".cr3", ".crw", ".nef", ".nrw",
    ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw",
    ".tif", ".tiff",
}
TIFF_RAW_EXTS = {".cr2", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
                 ".pef", ".srw", ".tif", ".tiff", ".orf", ".rw2"}

u16 = lambda b, o: struct.unpack_from("<H", b, o)[0]
u32 = lambda b, o: struct.unpack_from("<I", b, o)[0]
u64 = lambda b, o: struct.unpack_from("<Q", b, o)[0]


class UserError(Exception):
    """A problem the user can fix; shown as a one-line message, not a traceback."""


# --------------------------------------------------------------------------- I/O
class Image:
    """Read-only random access to an image file or a raw device."""

    def __init__(self, path, size=None, block=0):
        self.block = block                                 # raw devices only accept block-aligned reads
        self.f = open(path, "rb", buffering=0 if block else -1)
        if size is None:
            self.f.seek(0, os.SEEK_END)
            size = self.f.tell()
        self.size = size

    def read_at(self, off, n):
        n = min(n, self.size - off)                        # corrupt sizes can't trigger huge reads
        if off < 0 or n <= 0:
            return b""
        if not self.block:
            self.f.seek(off)
            return self.f.read(n)
        b = self.block
        start, end = off - off % b, -(-(off + n) // b) * b
        try:
            self.f.seek(start)
            data = self.f.read(end - start)
        except OSError:                                    # bad sectors: zero-fill the unreadable blocks
            data = b"".join(self._block(p) for p in range(start, end, b))
        return data[off - start:off - start + n]

    def _block(self, p):
        try:
            self.f.seek(p)
            return self.f.read(self.block)
        except OSError:
            return b"\x00" * self.block


@dataclass
class Entry:
    name: str
    size: int
    cluster: int
    deleted: bool
    is_dir: bool
    nofat: bool = False
    path: str = ""


# ------------------------------------------------------------------- filesystems
class Volume:
    base = end = data_off = csize = max_cluster = root_cluster = 0

    def cluster_off(self, c):
        return self.data_off + (c - 2) * self.csize

    def start_free(self, off):
        """True if `off` is the first byte of an unallocated cluster."""
        if off < self.data_off:
            return False
        rel = off - self.data_off
        if rel % self.csize:
            return False
        c = rel // self.csize + 2
        return True if c > self.max_cluster else self.is_free(c)

    def walk(self):
        self._seen = {self.root_cluster}                   # folder clusters visited; stops directory loops
        yield from self._walk(self._root(), "", 0, False)

    def _walk(self, data, path, depth, in_deleted):
        for e in self._parse(data):
            e.path = f"{path}/{e.name}" if path else e.name
            e.deleted = e.deleted or in_deleted
            if e.is_dir:
                if depth >= 16 or e.cluster < 2 or e.cluster in self._seen:
                    continue
                self._seen.add(e.cluster)
                sub = self._read_dir(e)
                if sub:
                    yield from self._walk(sub, e.path, depth + 1, e.deleted)
            else:
                yield e


class FAT(Volume):
    def __init__(self, img, base):
        b = img.read_at(base, 512)
        if len(b) < 512 or b[510:512] != b"\x55\xaa":
            raise ValueError("no boot signature")
        self.img, self.base = img, base
        self.bps, self.spc = u16(b, 11), b[13]
        reserved, nfats = u16(b, 14), b[16]
        if (self.bps not in (512, 1024, 2048, 4096) or self.spc == 0
                or self.spc & (self.spc - 1) or nfats not in (1, 2) or reserved == 0):
            raise ValueError("not FAT")
        self.root_entries = u16(b, 17)
        total = u16(b, 19) or u32(b, 32)
        spf = u16(b, 22) or u32(b, 36)
        root_secs = (self.root_entries * 32 + self.bps - 1) // self.bps
        self.csize = self.bps * self.spc
        self.fat_off = base + reserved * self.bps
        self.root_off = base + (reserved + nfats * spf) * self.bps
        self.data_off = self.root_off + root_secs * self.bps
        n = (total - (reserved + nfats * spf + root_secs)) // self.spc
        if n < 4085:
            raise ValueError("FAT12 not supported")
        self.max_cluster = n + 1
        self.fat32 = self.root_entries == 0
        self.root_cluster = u32(b, 44) if self.fat32 else 0    # FAT16 has no root cluster
        self.end = base + total * self.bps
        self.esz = 4 if self.fat32 else 2
        self.label = "FAT32" if self.fat32 else "FAT16"
        self.fat = img.read_at(self.fat_off, spf * self.bps)

    def _next(self, c):
        o = c * self.esz
        if o + self.esz > len(self.fat):
            return None
        return (u32(self.fat, o) & 0x0FFFFFFF) if self.fat32 else u16(self.fat, o)

    def is_free(self, c):
        return self._next(c) == 0

    def read_chain(self, c, limit=64 << 20):
        out, eoc = [], (0x0FFFFFF8 if self.fat32 else 0xFFF8)
        for _ in range(max(1, limit // self.csize)):
            if not 2 <= c <= self.max_cluster:
                break
            out.append(self.img.read_at(self.cluster_off(c), self.csize))
            c = self._next(c)
            if c is None or c == 0 or c >= eoc:
                break
        return b"".join(out)

    def _root(self):
        if self.fat32:
            return self.read_chain(self.root_cluster)
        return self.img.read_at(self.root_off, self.root_entries * 32)

    def _read_dir(self, e):
        if not e.deleted:
            return self.read_chain(e.cluster)
        # Deleted folder: FAT chain is wiped, so assume it fit in one cluster.
        if not self.is_free(e.cluster):
            return b""
        sub = self.img.read_at(self.cluster_off(e.cluster), self.csize)
        return sub if sub[:1] == b"." else b""

    @staticmethod
    def _lfn_sum(short):
        s = 0
        for ch in short:
            s = (((s & 1) << 7) + (s >> 1) + ch) & 0xFF
        return s

    def _parse(self, data):
        lfn = []
        for i in range(0, len(data) - 31, 32):
            e = data[i:i + 32]
            if e[0] == 0:
                break
            if e[11] & 0x3F == 0x0F:                       # long-name fragment
                lfn.append((e[13], e[1:11] + e[14:26] + e[28:32]))
                continue
            parts, lfn = lfn, []
            attr = e[11]
            if attr & 0x08 or e[0] == 0x2E:                # volume label, . and ..
                continue
            deleted = e[0] == 0xE5
            short = bytes(e[:11])
            base_ = short[:8].decode("cp437", "replace").rstrip()
            ext = short[8:].decode("cp437", "replace").rstrip()
            if deleted:
                base_ = "_" + base_[1:]                    # first char is lost
            name = base_ + ("." + ext if ext else "")
            if parts and len({p[0] for p in parts}) == 1 and (
                    deleted or parts[0][0] == self._lfn_sum(short)):
                raw = b"".join(p[1] for p in reversed(parts))
                long_name = raw.decode("utf-16le", "ignore").split("\x00")[0]
                name = long_name or name
            cl = u16(e, 26) | ((u16(e, 20) << 16) if self.fat32 else 0)
            yield Entry(name, u32(e, 28), cl, deleted, bool(attr & 0x10))


class ExFAT(Volume):
    label = "exFAT"

    def __init__(self, img, base):
        b = img.read_at(base, 512)
        if len(b) < 512 or b[3:11] != b"EXFAT   ":
            raise ValueError("not exFAT")
        self.img, self.base = img, base
        bps = 1 << b[108]
        self.csize = bps << b[109]
        self.fat_off = base + u32(b, 80) * bps
        self.data_off = base + u32(b, 88) * bps
        self.max_cluster = u32(b, 92) + 1
        self.root_cluster = u32(b, 96)
        self.end = base + u64(b, 72) * bps
        self.fat = img.read_at(self.fat_off, (self.max_cluster + 1) * 4)
        self.bitmap = b""
        root = self.read_chain(self.root_cluster, 1 << 24)
        for i in range(0, len(root) - 31, 32):
            if root[i] == 0x81:                            # allocation bitmap
                self.bitmap = img.read_at(self.cluster_off(u32(root, i + 20)),
                                          u64(root, i + 24))
                break
        if not self.bitmap:
            raise ValueError("exFAT bitmap not found")

    def is_free(self, c):
        if not 2 <= c <= self.max_cluster:
            return False
        i = (c - 2) >> 3
        return i < len(self.bitmap) and not (self.bitmap[i] >> ((c - 2) & 7)) & 1

    def read_chain(self, c, size, nofat=False):
        if nofat:
            return self.img.read_at(self.cluster_off(c), size)
        out = []
        for _ in range(max(1, size // self.csize + 1)):
            if not 2 <= c <= self.max_cluster:
                break
            out.append(self.img.read_at(self.cluster_off(c), self.csize))
            c = u32(self.fat, c * 4) if c * 4 + 4 <= len(self.fat) else 0
            if c == 0 or c >= 0xFFFFFFF7:
                break
        return b"".join(out)

    def _root(self):
        return self.read_chain(self.root_cluster, 1 << 24)

    def _read_dir(self, e):
        size = min(e.size, 1 << 24)
        if e.deleted:
            return self.read_chain(e.cluster, size, True) if self.is_free(e.cluster) else b""
        return self.read_chain(e.cluster, size, e.nofat)

    def _parse(self, data):
        i = 0
        while i + 32 <= len(data):
            t = data[i]
            if t == 0:
                break
            if t in (0x85, 0x05):                          # file entry set (live/deleted)
                cnt = data[i + 1]
                grp = data[i:i + 32 * (cnt + 1)]
                if cnt >= 2 and len(grp) == 32 * (cnt + 1) and grp[32] in (0xC0, 0x40):
                    s = grp[32:64]
                    nlen = s[3]
                    name = b"".join(grp[32 * k + 2:32 * k + 32] for k in range(2, cnt + 1))
                    name = name.decode("utf-16le", "ignore")[:nlen]
                    yield Entry(name, u64(s, 24), u32(s, 20), t == 0x05,
                                bool(u16(grp, 4) & 0x10), bool(s[1] & 0x02))
                    i += 32 * (cnt + 1)
                    continue
            i += 32


def find_volumes(img):
    def attempt(base):
        for cls in (ExFAT, FAT):
            try:
                return cls(img, base)
            except Exception:
                pass
        return None

    v = attempt(0)
    if v:
        return [v]
    vols, mbr = [], img.read_at(0, 512)
    if len(mbr) == 512 and mbr[510:512] == b"\x55\xaa":
        for i in range(4):
            e = mbr[446 + 16 * i:462 + 16 * i]
            ptype, lba = e[4], u32(e, 8)
            if ptype == 0xEE:                              # GPT
                hdr = img.read_at(512, 92)
                if len(hdr) < 92 or hdr[:8] != b"EFI PART":
                    continue
                n, sz = u32(hdr, 80), u32(hdr, 84)
                if not (1 <= n <= 1024 and 128 <= sz <= 4096):     # corrupt header
                    continue
                ents = img.read_at(u64(hdr, 72) * 512, n * sz)
                for k in range(n):
                    ent = ents[k * sz:(k + 1) * sz]
                    if len(ent) >= 40 and any(ent[:16]):
                        v = attempt(u64(ent, 32) * 512)
                        if v:
                            vols.append(v)
            elif ptype and lba:
                v = attempt(lba * 512)
                if v:
                    vols.append(v)
    return vols


# ---------------------------------------------------------------------- carving
def classify(h):
    """Identify a photo header from the first 16 bytes of a sector."""
    c = h[0]
    if c == 0xFF:
        return "jpeg" if h[:3] == b"\xff\xd8\xff" else None
    if c == 0x89:
        return "png" if h[:8] == PNG_SIG else None
    if c == 0x49:                                          # 'I'
        t = h[:4]
        return ("tiff" if t == b"II*\x00" else "orf" if t in (b"IIRO", b"IIRS")
                else "rw2" if t == b"IIU\x00" else None)
    if c == 0x4D:
        return "tiff" if h[:4] == b"MM\x00*" else None
    if c == 0x46:
        return "raf" if h[:8] == b"FUJIFILM" else None
    if c == 0x00:
        return "cr3" if h[4:12] == b"ftypcrx " else None
    return None


_MARKER = re.compile(rb"\xff[\x01-\xcf\xd8-\xfe]")         # a real JPEG marker


def jpeg_end(img, start, limit):
    """Walk JPEG segments (skipping embedded thumbnails) to find the true EOI."""
    pos = start + 2
    while pos - start < limit:
        b = img.read_at(pos, 4)
        if len(b) < 2 or b[0] != 0xFF:
            return None
        m = b[1]
        if m == 0xFF:
            pos += 1
        elif m == 0x01 or 0xD0 <= m <= 0xD8:
            pos += 2
        elif m == 0xD9:
            return pos + 2 - start
        else:
            if len(b) < 4:
                return None
            ln = (b[2] << 8) | b[3]
            if ln < 2:
                return None
            pos += 2 + ln
            if m == 0xDA:                                  # scan data until next marker
                p = pos
                while True:
                    if p - start >= limit:
                        return None
                    blk = img.read_at(p, 1 << 20)
                    if len(blk) < 2:
                        return None
                    hit = _MARKER.search(blk)
                    if hit:
                        pos = p + hit.start()
                        break
                    p += len(blk) - 1
    return None


def png_end(img, start, limit):
    if img.read_at(start + 12, 4) != b"IHDR":
        return None
    pos = start + 8
    while pos - start < limit:
        h = img.read_at(pos, 8)
        if len(h) < 8:
            return None
        ln, typ = struct.unpack(">I4s", h)
        if ln > 0x7FFFFFFF or not typ.isalpha():
            return None
        pos += 12 + ln
        if typ == b"IEND":
            return pos - start
    return None


def bmff_end(img, start, limit):
    """CR3 (ISO base media): sum top-level boxes. May exceed `limit`; measure() checks."""
    pos = start
    while pos - start < limit:
        h = img.read_at(pos, 16)
        if len(h) < 8:
            break
        sz, typ = struct.unpack(">I4s", h[:8])
        if not all(32 <= c < 127 for c in typ):
            break
        if sz == 1 and len(h) == 16:
            sz = struct.unpack(">Q", h[8:16])[0]
        if sz < 8:
            break
        pos += sz
    return pos - start if pos - start > 4096 else None


def raf_len(img, start):
    """RAF: total length from the header's preview/raw offsets. measure() checks the limit."""
    h = img.read_at(start, 112)
    if len(h) < 108:
        return None
    jo, jl, _, _, co, cl = struct.unpack(">6I", h[84:108])
    total = max(jo + jl, co + cl)
    return total if total > 4096 else None


def tiff_ok(img, start):
    h = img.read_at(start, 8)
    if len(h) < 8:
        return False
    ifd = struct.unpack("<I" if h[:2] == b"II" else ">I", h[4:8])[0]
    if not 8 <= ifd < (1 << 26):
        return False
    n = img.read_at(start + ifd, 2)
    return len(n) == 2 and 1 <= struct.unpack("<H" if h[:2] == b"II" else ">H", n)[0] <= 1000


def approx_end(img, start, limit):
    """Length for formats without a simple end marker: run to next header or zero padding.
    Returns (length, True if the data was still going when `limit` was reached)."""
    pos, last, zeros = start + SECTOR, start + SECTOR, 0
    while pos - start < limit:
        blk = img.read_at(pos, min(1 << 20, start + limit - pos))
        if not blk:
            break
        for o in range(0, len(blk), SECTOR):
            sec = blk[o:o + SECTOR]
            if classify(sec[:16]):
                return last - start, False
            if sec.count(0) == len(sec):
                zeros += 1
                if zeros >= 8:
                    return last - start, False
            else:
                zeros, last = 0, pos + o + len(sec)
        pos += len(blk)
    return last - start, pos - start >= limit


def measure(img, kind, off):
    """Return (length, problem). length None = no usable file; problem says why, for the report."""
    room = img.size - off
    if kind == "jpeg":
        return jpeg_end(img, off, min(MAX_JPEG, room)), ""
    if kind == "png":
        return png_end(img, off, min(MAX_PNG, room)), ""
    if kind in ("cr3", "raf"):
        n = bmff_end(img, off, min(MAX_BMFF, room)) if kind == "cr3" else raf_len(img, off)
        lim = MAX_BMFF if kind == "cr3" else MAX_RAW
        if n and n > room:
            return None, "header says the file runs past the end of the card - damaged or cut off"
        if n and n > lim:
            return None, f"larger than the {lim >> 20} MiB size limit, or header damaged"
        return n, ""
    if kind == "tiff" and not tiff_ok(img, off):
        return None, ""
    lim = min(MAX_RAW, room)
    n, full = approx_end(img, off, lim)
    return n, (f"cut at the {MAX_RAW >> 20} MiB size limit" if full and lim == MAX_RAW else "")


def carved_ext(kind, data):
    if kind == "tiff":
        return ".cr2" if data[8:10] == b"CR" else ".tif"   # NEF/ARW/DNG also land here
    return {"jpeg": ".jpg", "png": ".png", "raf": ".raf", "cr3": ".cr3",
            "orf": ".orf", "rw2": ".rw2"}[kind]


def sig_ok(ext, head):
    if ext in (".jpg", ".jpeg", ".jpe"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return head[:8] == PNG_SIG
    if ext in TIFF_RAW_EXTS:
        return head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"IIRS", b"IIU\x00")
    if ext == ".raf":
        return head[:8] == b"FUJIFILM"
    if ext == ".cr3":
        return head[4:12] == b"ftypcrx "
    return True


# ---------------------------------------------------------------------- helpers
def safe_path(root, rel):
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).strip(" .") or "_" for p in rel.split("/")]
    return root.joinpath(*parts)


def unique(p):
    if not p.exists():
        return p
    i = 1
    while p.with_name(f"{p.stem}_{i}{p.suffix}").exists():
        i += 1
    return p.with_name(f"{p.stem}_{i}{p.suffix}")


def verify_ok(path):
    """Decode the picture with Pillow if available; True when OK or unverifiable."""
    if path.suffix.lower() not in (".jpg", ".png"):
        return True
    try:
        from PIL import Image as PILImage
    except ImportError:
        return True
    try:
        with PILImage.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


def move_if_broken(dest, out, rel, a):
    """With --verify, move a picture Pillow can't decode into suspect/. Returns (path, ok)."""
    if not a.verify or verify_ok(dest):
        return dest, True
    new = unique(safe_path(out / "suspect", rel))
    new.parent.mkdir(parents=True, exist_ok=True)
    return dest.rename(new), False


def write_range(img, off, n, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with open(dest, "wb") as f:
        done = 0
        while done < n:
            buf = img.read_at(off + done, min(4 << 20, n - done))
            if not buf:
                break
            h.update(buf)
            f.write(buf)
            done += len(buf)
    return h.hexdigest()


def give_back(paths):
    """Under sudo, hand files we created back to the real user (otherwise only root could delete them)."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or not gid or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    for top in map(Path, paths):
        if not top.exists():
            continue
        for p in [top, *(top.rglob("*") if top.is_dir() else ())]:
            try:
                os.lchown(p, int(uid), int(gid))
            except OSError:
                pass


class Report:
    """report.csv (one row per file, written as we go so a crash keeps it) and
    summary.csv (three columns: recovered / failed / cannot recover), written at the end."""

    def __init__(self, out):
        self.out = out
        self.f = open(out / "report.csv", "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(["result", "method", "original_name", "saved_as", "byte_offset",
                         "size", "detail", "sha256"])
        self.lists = {OK: [], FAILED: [], CANNOT: []}

    def add(self, result, method, original, saved, off, size, detail, digest=""):
        self.w.writerow([result, method, original, str(saved), off, size, detail, digest])
        self.f.flush()
        name = str(saved or original)
        self.lists[result].append(name if result == OK else f"{name} ({detail})")

    def close(self):
        self.f.close()
        with open(self.out / "summary.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(list(self.lists))
            w.writerows(zip_longest(*self.lists.values(), fillvalue=""))


# ---------------------------------------------------------------------- phases
def recover_fs(vol, img, out, a, hashes, report):
    for e in vol.walk():
        if not e.deleted or e.size == 0:
            continue
        ext = Path(e.name).suffix.lower()
        if not a.all_types and ext not in PHOTO_EXTS:
            continue
        need = -(-e.size // vol.csize)
        first = e.cluster
        if first < 2 or first + need - 1 > vol.max_cluster:
            report.add(CANNOT, "filesystem", e.path, "", "", e.size, "directory entry damaged - bad cluster range")
            continue
        if not vol.is_free(first):
            report.add(CANNOT, "filesystem", e.path, "", "", e.size, "overwritten by newer data")
            continue
        off = vol.cluster_off(first)
        if not sig_ok(ext, img.read_at(off, 16)):
            report.add(CANNOT, "filesystem", e.path, "", off, e.size, "overwritten - data at its location is something else")
            continue
        free = sum(vol.is_free(c) for c in range(first, first + need))
        dest = unique(safe_path(out / "filesystem", e.path))
        try:
            digest = write_range(img, off, e.size, dest)
            dest, ok = move_if_broken(dest, out, e.path, a)
        except OSError as ex:
            if ex.errno in DISK_FULL:
                raise
            report.add(FAILED, "filesystem", e.path, "", off, e.size, f"could not save: {ex}")
            continue
        hashes.add(digest)
        if not ok:
            result, detail = FAILED, "saved to suspect/: picture does not decode"
        elif free != need:
            result, detail = FAILED, "saved, but partly overwritten - probably corrupt"
        else:
            result, detail = OK, "intact"
        report.add(result, "filesystem", e.path, dest.relative_to(out), off, e.size, detail, digest)


def carve(img, vols, out, a, hashes, report):
    def allowed(off):
        for v in vols:
            if v.base <= off < v.end:
                return v.start_free(off)
        return True

    found, resume, total = 0, 0, img.size
    for base in range(0, total, CHUNK):
        chunk = img.read_at(base, CHUNK + 16)
        for o in range(0, min(CHUNK, total - base), SECTOR):
            h = chunk[o:o + 16]
            if not h:
                break
            kind = classify(h)
            off = base + o
            if not kind or off < resume or not allowed(off):
                continue
            length, problem = measure(img, kind, off)
            if not length:
                if problem:
                    report.add(CANNOT, "carving", f"{kind} at byte {off:,}", "", off, "", problem)
                continue
            if length < a.min_size:
                continue
            data = img.read_at(off, length)
            digest = hashlib.sha256(data).hexdigest()
            resume = off + length
            if digest in hashes:
                continue
            hashes.add(digest)
            dest = out / "carved" / f"f_{off:012x}{carved_ext(kind, data)}"
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                dest, ok = move_if_broken(dest, out, dest.name, a)
            except OSError as ex:
                if ex.errno in DISK_FULL:
                    raise
                report.add(FAILED, "carving", "", dest.relative_to(out), off, length, f"could not save: {ex}")
                continue
            if not ok:
                result, detail = FAILED, "saved to suspect/: picture does not decode"
            elif problem:
                result, detail = FAILED, f"saved, but incomplete: {problem}"
            elif kind in ("jpeg", "png", "cr3", "raf"):
                result, detail = OK, "carved"
            else:
                result, detail = OK, "carved - length estimated, format has no end marker"
            found += 1
            report.add(result, "carving", "", dest.relative_to(out), off, length, detail, digest)
        print(f"\r  carving: {min(base + CHUNK, total) * 100 // total:3d}%  "
              f"({found} found)", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)


def run_recovery(img, out, a):
    out.mkdir(parents=True, exist_ok=True)
    try:
        vols = find_volumes(img)
    except Exception as ex:                                # damaged partition table
        print(f"warning: partition table unreadable ({ex})")
        vols = []
    if vols:
        for v in vols:
            print(f"Found {v.label} volume at byte {v.base:,} (cluster {v.csize:,} B)")
    else:
        print("No readable FAT/exFAT volume (formatted or damaged?) - carving only.")
    report, hashes = Report(out), set()
    try:
        for v in vols:
            print(f"Phase 1: scanning {v.label} for deleted files...")
            try:
                recover_fs(v, img, out, a, hashes, report)
            except Exception as ex:                        # damaged directory structures
                if isinstance(ex, OSError) and ex.errno in DISK_FULL:
                    raise
                print(f"  warning: directory scan stopped early ({ex})")
        if not a.no_carve:
            print("Phase 2: carving free space for photo signatures...")
            carve(img, vols, out, a, hashes, report)
    finally:
        report.close()
        give_back([out / n for n in ("filesystem", "carved", "suspect", "report.csv", "summary.csv")])
        n = {k: len(v) for k, v in report.lists.items()}
        print(f"\n{OK}: {n[OK]}   {FAILED}: {n[FAILED]}   {CANNOT}: {n[CANNOT]}")
        print(f"Files:   {out.resolve()}/")
        print(f"Details: {out / 'report.csv'} and {out / 'summary.csv'}")


# ------------------------------------------------------------------ macOS disks
def diskutil(verb, *args):
    try:
        r = subprocess.run(["diskutil", verb, "-plist", *args], capture_output=True, check=True)
    except FileNotFoundError:
        raise UserError("diskutil not found: choosing a card by name works on macOS only") from None
    except subprocess.CalledProcessError:
        raise UserError(f"diskutil could not find {' '.join(args) or verb}") from None
    return plistlib.loads(r.stdout)


@dataclass
class Disk:
    ident: str                                             # whole-disk id, e.g. disk6
    size: int
    block: int
    bus: str
    own: bool                                              # holds this Mac's system: never read
    names: list = field(default_factory=list)
    mounts: list = field(default_factory=list)


def list_disks():
    """Every whole disk macOS knows about, with the Mac's own disks flagged."""
    boot = diskutil("info", "/")
    system = {boot.get("ParentWholeDisk")}
    system |= {diskutil("info", s["APFSPhysicalStore"]).get("ParentWholeDisk")
               for s in boot.get("APFSPhysicalStores", [])}
    disks = []
    for d in diskutil("list")["AllDisksAndPartitions"]:
        info = diskutil("info", d["DeviceIdentifier"])
        parts = [d] + d.get("Partitions", []) + d.get("APFSVolumes", [])
        # Built-in SD slots report "internal" too, so only internal *fixed* disks count as the Mac's own.
        own = d["DeviceIdentifier"] in system or bool(info.get("Internal") and not info.get("RemovableMedia"))
        disks.append(Disk(d["DeviceIdentifier"], info.get("Size") or info.get("TotalSize") or 0,
                          info.get("DeviceBlockSize") or SECTOR, info.get("BusProtocol", "?"), own,
                          [p["VolumeName"] for p in parts if p.get("VolumeName")],
                          [p["MountPoint"] for p in parts if p.get("MountPoint")]))
    return disks


def mac_device(path):
    """(size, block size, whole Disk) for /dev/[r]diskN[sM]; refuses the Mac's own disks."""
    m = re.fullmatch(r"/dev/r?(disk\d+(?:s\d+)*)", path)
    if not m:
        raise UserError(f"{path}: expected a disk device such as /dev/rdisk4")
    info = diskutil("info", m.group(1))
    disk = next((d for d in list_disks() if d.ident == info.get("ParentWholeDisk")), None)
    if disk is None or disk.own:
        raise UserError(f"{path} is one of this Mac's own disks - refusing to read it")
    return info.get("Size") or info.get("TotalSize") or 0, info.get("DeviceBlockSize") or SECTOR, disk


def refuse_on_card(path, disk):
    """Never write the image or recovered files onto the card being recovered."""
    p = Path(path).expanduser().resolve()
    for m in map(Path, disk.mounts):
        if p == m or m in p.parents:
            raise UserError(f"{path} is on the card itself - choose a folder on your Mac "
                            "(writing to the card destroys deleted files)")


def is_device(path):
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def open_source(path, out):
    """Open an image file, or a device (with its real size and block alignment)."""
    if not is_device(path):
        img = Image(path)
        if not img.size:
            raise UserError(f"{path} is empty")
        return img
    if sys.platform == "darwin":
        size, block, disk = mac_device(path)
        refuse_on_card(out, disk)
        return Image(path, size, block)
    img = Image(path)
    if not img.size:
        raise UserError(f"can't tell the size of {path} - copy it with the image command first")
    return img


def copy_device(src_path, dest, total, block=SECTOR, mode="xb"):
    """Sector-by-sector copy; unreadable sectors become zeros. Returns (bytes copied, bad sectors)."""
    bad, off = 0, 0
    with open(src_path, "rb", buffering=0) as src, open(dest, mode) as d:
        while not total or off < total:
            n = min(1 << 20, total - off) if total else 1 << 20
            try:
                src.seek(off)
                buf = src.read(n)
            except OSError:                                # unreadable area: go sector by sector
                buf, failed = b"", 0
                for s in range(0, n, block):
                    try:
                        src.seek(off + s)
                        x = src.read(block)
                    except OSError:
                        x, failed = b"\x00" * block, failed + 1
                    if not x:
                        break
                    buf += x
                if not total and failed * block >= n:      # size unknown and nothing readable: the end
                    break
                bad += failed
            if not buf:
                break
            d.write(buf)
            off += len(buf)
            pct = f"{off * 100 // total:3d}%" if total else f"{off >> 20} MiB"
            print(f"\r  copying: {pct}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return off, bad


# -------------------------------------------------------------------- commands
def cmd_scan(a):
    if sys.platform != "darwin":
        raise UserError("choosing a card by name needs macOS - use the image and recover commands instead")
    disks = [d for d in list_disks() if not d.own and (a.allow_disk_images or d.bus != "Disk Image")]
    if not disks:
        raise UserError("no SD card or external disk found - insert the card and try again")
    print("Cards and external disks (your Mac's own disks are never listed or read):")
    for d in disks:
        print(f"  {', '.join(d.names) or '(no name)':<28} {d.ident:<8} {d.size / 1e9:7.1f} GB  {d.bus}")
    name = a.volume or input("\nName of the card to scan (or its disk id, e.g. disk4): ").strip()
    hits = [d for d in disks
            if name.casefold() in (n.casefold() for n in d.names) or name == d.ident]
    if not hits:
        raise UserError(f"'{name}' is not one of the disks listed above")
    if len(hits) > 1:
        raise UserError(f"several disks have a volume called '{name}' - type the disk id instead "
                        f"({', '.join(d.ident for d in hits)})")
    disk, dev = hits[0], f"/dev/r{hits[0].ident}"
    out = Path(a.output).expanduser().resolve()
    refuse_on_card(out, disk)
    print(f"\nThe whole card ({disk.ident}, {disk.size / 1e9:.1f} GB) will be read. Results go to {out}/")
    if not a.volume and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled - nothing was read.")
        return
    if not os.access(dev, os.R_OK):
        raise UserError(f"no permission to read {dev} - run it again with sudo")
    created = not out.exists()
    out.mkdir(parents=True, exist_ok=True)
    try:
        img, copy = card_source(disk, dev, out, a)
        run_recovery(img, out, a)
        if copy:
            print(f"\nThe card copy is {copy}. To scan it again without touching the card:\n"
                  f"  python3 {shlex.quote(sys.argv[0])} recover {shlex.quote(str(copy))} -o {shlex.quote(str(out) + '_2')}\n"
                  "Delete the copy when you're done; it is as big as the card.")
    finally:
        give_back([out] if created else [])


def card_source(disk, dev, out, a):
    """Copy the card into `out` when there's room (one pass over the card, repeatable);
    otherwise read the card directly. Returns (Image, path of the copy or None)."""
    if not a.direct:
        label = re.sub(r"[^\w.-]", "_", disk.names[0] if disk.names else disk.ident)
        copy = unique(out / f"{label}.img")
        free = shutil.disk_usage(out).free
        if free >= disk.size + (1 << 30):
            part = copy.with_name(copy.name + ".part")
            print(f"Copying the card to {copy} ...")
            try:
                done, bad = copy_device(dev, part, disk.size, disk.block, "wb")
            finally:
                give_back([part])
            if done < disk.size:
                raise UserError(f"the card stopped responding after {done:,} bytes - partial copy kept in {part}")
            part.rename(copy)
            print(f"Copied {done:,} bytes" + (f" ({bad} unreadable sectors zero-filled)" if bad else ""))
            return Image(copy), copy
        print(f"Not enough free space to copy the card first ({disk.size / 1e9:.1f} GB needed, "
              f"{free / 1e9:.1f} GB free) - reading the card directly.")
    return Image(dev, disk.size, disk.block), None


def cmd_image(a):
    dest = Path(a.dest).expanduser()
    if dest.exists() or dest.resolve().parts[1:2] == ("dev",):
        raise UserError(f"{dest} already exists - choose a new file name (nothing was written)")
    total, block = 0, SECTOR
    if is_device(a.source) and sys.platform == "darwin":
        total, block, disk = mac_device(a.source)
        refuse_on_card(dest, disk)
    else:
        try:
            with open(a.source, "rb") as f:
                total = f.seek(0, os.SEEK_END)
        except OSError:                                    # e.g. Windows devices can't report a size
            total = 0
    try:
        done, bad = copy_device(a.source, dest, total, block)
    finally:
        give_back([dest])
    print(f"Done: {done:,} bytes written to {dest}" +
          (f" ({bad} unreadable sectors zero-filled)" if bad else ""))
    if total and done < total:
        print(f"warning: the source stopped responding after {done:,} of {total:,} bytes - the copy is incomplete")


def cmd_recover(a):
    out = Path(a.output).expanduser()
    run_recovery(open_source(a.image, out), out, a)


def main():
    p = argparse.ArgumentParser(description="Recover deleted photos from an SD card (read-only). "
                                            "On macOS, run it with no arguments to pick the card by name.")
    sub = p.add_subparsers(dest="cmd", required=True)
    opts = argparse.ArgumentParser(add_help=False)         # options shared by scan and recover
    opts.add_argument("-o", "--output", default="recovered",
                      help="folder for recovered files and reports (default: ./recovered)")
    opts.add_argument("--all-types", action="store_true",
                      help="filesystem phase: recover every deleted file, not just photos")
    opts.add_argument("--no-carve", action="store_true", help="skip signature carving")
    opts.add_argument("--min-size", type=int, default=10 * 1024,
                      help="ignore carved files smaller than this many bytes (default 10240)")
    opts.add_argument("--verify", action="store_true",
                      help="decode JPEG/PNG with Pillow; move broken ones to suspect/")
    s = sub.add_parser("scan", parents=[opts],
                       help="macOS: pick the card by name, then copy and recover it (the default)")
    s.add_argument("--volume", help="the card's name as shown in Finder (skips the questions)")
    s.add_argument("--direct", action="store_true", help="read the card directly instead of copying it first")
    s.add_argument("--allow-disk-images", action="store_true", help="also offer mounted disk images (.dmg)")
    s.set_defaults(fn=cmd_scan)
    i = sub.add_parser("image", help="make a sector-by-sector copy of the card")
    i.add_argument("source", help="device, e.g. /dev/rdisk4 or /dev/sdb")
    i.add_argument("dest", help="new .img file (needs free space = card size)")
    i.set_defaults(fn=cmd_image)
    r = sub.add_parser("recover", parents=[opts], help="recover files from an image")
    r.add_argument("image", help="image file made by the image command (a device path also works)")
    r.set_defaults(fn=cmd_recover)
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("scan", "image", "recover", "-h", "--help"):
        argv = ["scan", *argv]                            # no command given: pick the card by name
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except UserError as ex:
        sys.exit(f"error: {ex}")
    except KeyboardInterrupt:
        sys.exit("\nStopped. Everything saved so far is kept.")
    except EOFError:
        sys.exit("\nerror: no answer given")
    except PermissionError as ex:
        sys.exit(f"error: {ex}\nReading a card needs administrator rights: run it again with sudo. If you already "
                 "did, give your terminal app Full Disk Access (System Settings > Privacy & Security).")
    except OSError as ex:
        if ex.errno in DISK_FULL:
            sys.exit("error: no space left where the files are being saved - free some space and run again (files saved so far are kept)")
        sys.exit(f"error: {ex}")


if __name__ == "__main__":
    main()
