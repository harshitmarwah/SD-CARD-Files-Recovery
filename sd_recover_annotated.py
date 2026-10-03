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
# ============================================================================
# ANNOTATED COPY of sd_recover.py: the code is identical, only comments were added.
# Read this file to learn how the program works; run sd_recover.py.
#
# Background used throughout:
#   * Sector: the smallest unit a disk reads (512 bytes on SD cards).
#   * Cluster: a group of sectors; filesystems hand out space in whole clusters.
#   * FAT/exFAT record which clusters are free in a table (the "FAT") or a bitmap.
#     Deleting a file only flags its directory entry as deleted and marks its
#     clusters free - the bytes stay until something overwrites them. That is
#     why recovery is possible, and why you must stop writing to the card.
#   * Carving: ignoring the filesystem and searching raw bytes for file headers.
#   * Raw device: /dev/rdiskN on macOS - the whole card as one long run of bytes,
#     deleted areas included. Reading it needs administrator rights (sudo).
# ============================================================================
# Imports: all from Python's standard library, so there is nothing to install.
# argparse: command-line commands and options
import argparse
# csv: writes report.csv and summary.csv
import csv
# errno: names for OS error codes (used to spot 'disk full')
import errno
# hashlib: SHA-256 fingerprints, used to skip duplicate files
import hashlib
# os: file sizes, permissions, ownership (chown) and environment variables
import os
# plistlib: parses the property-list output of macOS 'diskutil -plist'
import plistlib
# re: regular expressions (JPEG markers, device names, safe file names)
import re
# shlex: quotes paths safely when printing a command to copy and paste
import shlex
# shutil: free disk space check
import shutil
# stat: tells devices apart from ordinary files
import stat
# struct: unpacks binary integers from raw bytes
import struct
# subprocess: runs the macOS 'diskutil' tool
import subprocess
# sys: command-line arguments, progress output on stderr, platform, exit
import sys
# dataclass: compact record classes (Entry, Disk)
from dataclasses import dataclass, field
# zip_longest: lines up the three summary.csv columns, which have different lengths
from itertools import zip_longest
# Path: object-oriented file paths
from pathlib import Path

# ---- constants ----
# SECTOR: logical sector size assumed for SD cards, in bytes.
SECTOR = 512
# The fixed 8-byte signature every PNG file starts with.
PNG_SIG = b"\x89PNG\r\n\x1a\n"
# Size caps for carving. '<< 20' multiplies by 1,048,576, so 64 << 20 = 64 MiB.
# A cap stops a damaged header from making the program read gigabytes.
MAX_JPEG = MAX_PNG = 64 << 20
# CR3 (ISO 'box' format) and other RAW files may be up to 256 MiB.
MAX_BMFF = MAX_RAW = 256 << 20         # uncompressed 100 MP RAW files approach 200 MiB
# Carving reads the card 8 MiB at a time.
CHUNK = 8 << 20

# The three result categories used in report.csv and summary.csv:
#   OK     = saved and complete
#   FAILED = saved but damaged or incomplete, or saving failed
#   CANNOT = the data is gone or unusable, nothing saved
OK, FAILED, CANNOT = "Recovered successfully", "Failed to recover", "Cannot recover"
# OS error codes meaning the output disk is full (EDQUOT = quota exceeded; absent on Windows).
DISK_FULL = (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))

# Extensions Phase 1 restores unless --all-types is given.
PHOTO_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".cr2", ".cr3", ".crw", ".nef", ".nrw",
    ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw",
    ".tif", ".tiff",
}
# Extensions whose files start with a TIFF-style header (used to check headers).
TIFF_RAW_EXTS = {".cr2", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
                 ".pef", ".srw", ".tif", ".tiff", ".orf", ".rw2"}

# Little-endian integer readers: u16(buffer, offset) -> int.
# '<H' = unsigned 16-bit, '<I' = 32-bit, '<Q' = 64-bit. FAT and exFAT store numbers
# little-endian. They raise struct.error if the buffer is too short, so callers check lengths.
u16 = lambda b, o: struct.unpack_from("<H", b, o)[0]
u32 = lambda b, o: struct.unpack_from("<I", b, o)[0]
u64 = lambda b, o: struct.unpack_from("<Q", b, o)[0]


# Raised for problems the user can fix (wrong name, no permission, ...).
# main() prints these as one line instead of a Python traceback.
class UserError(Exception):
    """A problem the user can fix; shown as a one-line message, not a traceback."""


# --------------------------------------------------------------------------- I/O
# Image wraps an image file or a raw device and offers one operation, read_at().
# Everything reads through it, and it only ever opens files read-only.
class Image:
    """Read-only random access to an image file or a raw device."""

    # size/block are given for raw devices: macOS reports a raw device's size as 0, so the
    # real size comes from diskutil, and raw reads must be whole multiples of the block size.
    def __init__(self, path, size=None, block=0):
        self.block = block                                 # raw devices only accept block-aligned reads
        # 'rb' = read-only binary. Devices are opened unbuffered (buffering=0) because the normal
        # buffer would issue unaligned reads, which raw devices reject with EINVAL.
        self.f = open(path, "rb", buffering=0 if block else -1)
        # Ordinary files: find the size by seeking to the end.
        if size is None:
            self.f.seek(0, os.SEEK_END)
            size = self.f.tell()
        # read_at never reads past this size.
        self.size = size

    # Read n bytes at absolute byte offset `off`; fewer near the end, b'' if none.
    def read_at(self, off, n):
        # Clamp n, so a garbage size from a damaged header (e.g. 1 TiB) can't allocate a huge buffer.
        n = min(n, self.size - off)                        # corrupt sizes can't trigger huge reads
        # Nonsense ranges return nothing instead of raising.
        if off < 0 or n <= 0:
            return b""
        # Image files: a plain seek and read.
        if not self.block:
            self.f.seek(off)
            return self.f.read(n)
        # Devices: widen the request to whole blocks (round the start down, the end up),
        # read that, then slice out exactly the bytes asked for.
        b = self.block
        start, end = off - off % b, -(-(off + n) // b) * b
        # A read error means a bad sector somewhere in the range: fall back to one block at a time.
        try:
            self.f.seek(start)
            data = self.f.read(end - start)
        except OSError:                                    # bad sectors: zero-fill the unreadable blocks
            data = b"".join(self._block(p) for p in range(start, end, b))
        # Cut the requested bytes out of the aligned buffer.
        return data[off - start:off - start + n]

    # Read one block; an unreadable block comes back as zeros so recovery can continue.
    def _block(self, p):
        try:
            self.f.seek(p)
            return self.f.read(self.block)
        except OSError:
            return b"\x00" * self.block


# One directory entry (file or folder) found while walking the filesystem.
@dataclass
class Entry:
    # name: file name (the long name when one is available)
    name: str
    # size: size in bytes recorded in the entry
    size: int
    # cluster: first cluster of the file's data
    cluster: int
    # deleted: the entry is flagged deleted (or sits inside a deleted folder)
    deleted: bool
    # is_dir: the entry is a folder
    is_dir: bool
    # nofat: exFAT flag - the data is contiguous, so the FAT chain isn't used
    nofat: bool = False
    # path: full path on the card, filled in during the walk
    path: str = ""


# ------------------------------------------------------------------- filesystems
# Shared base for FAT and ExFAT; subclasses provide is_free, _root, _parse and _read_dir.
# base = volume start in the image, end = volume end, data_off = byte offset of cluster 2
# (the first data cluster), csize = cluster size in bytes, max_cluster = highest valid
# cluster number, root_cluster = root folder's first cluster (0 if it has none).
class Volume:
    base = end = data_off = csize = max_cluster = root_cluster = 0

    # Clusters are numbered from 2: cluster 2 sits at data_off, each next one csize later.
    def cluster_off(self, c):
        return self.data_off + (c - 2) * self.csize

    # Used by carving: photos are only looked for at the start of free clusters (files start there).
    def start_free(self, off):
        """True if `off` is the first byte of an unallocated cluster."""
        # Before the data area (boot sector, FAT, root folder): never a photo start.
        if off < self.data_off:
            return False
        rel = off - self.data_off
        # Not on a cluster boundary.
        if rel % self.csize:
            return False
        # Convert the byte distance back to a cluster number.
        c = rel // self.csize + 2
        # Past the last cluster counts as free (unclaimed space at the end of the card).
        return True if c > self.max_cluster else self.is_free(c)

    # Yield every file in the volume, deleted ones included.
    def walk(self):
        # Folder clusters already visited. Damaged or reused entries can point back at a
        # parent folder, which would otherwise loop for billions of steps.
        self._seen = {self.root_cluster}                   # folder clusters visited; stops directory loops
        # Start at the root folder: empty path, depth 0, not inside a deleted folder.
        yield from self._walk(self._root(), "", 0, False)

    # Recursive walk over one folder's raw bytes.
    def _walk(self, data, path, depth, in_deleted):
        for e in self._parse(data):
            # Full path, e.g. DCIM/101_FUJI/DSCF5414.RAF.
            e.path = f"{path}/{e.name}" if path else e.name
            # Anything inside a deleted folder is deleted too.
            e.deleted = e.deleted or in_deleted
            if e.is_dir:
                # Skip: too deep (16 levels), no real data (cluster < 2), or already visited (a loop).
                if depth >= 16 or e.cluster < 2 or e.cluster in self._seen:
                    continue
                # Mark it visited, read the sub-folder's bytes and recurse into it.
                self._seen.add(e.cluster)
                sub = self._read_dir(e)
                if sub:
                    yield from self._walk(sub, e.path, depth + 1, e.deleted)
            else:
                # A plain file: hand it to the caller (recover_fs).
                yield e


# FAT16 / FAT32 reader.
class FAT(Volume):
    def __init__(self, img, base):
        # Boot sector: the first 512 bytes of the volume.
        b = img.read_at(base, 512)
        # Every valid boot sector ends with the bytes 55 AA.
        if len(b) < 512 or b[510:512] != b"\x55\xaa":
            raise ValueError("no boot signature")
        # Keep the image and the volume's start offset.
        self.img, self.base = img, base
        # Bytes per sector (offset 11) and sectors per cluster (offset 13).
        self.bps, self.spc = u16(b, 11), b[13]
        # Reserved sectors before the FAT (offset 14) and number of FAT copies (offset 16).
        reserved, nfats = u16(b, 14), b[16]
        # Sanity checks so random bytes aren't mistaken for FAT: a legal sector size, a cluster
        # size that is a nonzero power of two (x & (x-1) is 0 only for powers of two), 1-2 FATs.
        if (self.bps not in (512, 1024, 2048, 4096) or self.spc == 0
                or self.spc & (self.spc - 1) or nfats not in (1, 2) or reserved == 0):
            raise ValueError("not FAT")
        # Number of fixed root-folder entries (FAT16); 0 on FAT32.
        self.root_entries = u16(b, 17)
        # Total sectors: the 16-bit field, or the 32-bit field when that is 0.
        total = u16(b, 19) or u32(b, 32)
        # Sectors per FAT: the 16-bit field, or FAT32's 32-bit field.
        spf = u16(b, 22) or u32(b, 36)
        # Size of the FAT16 root folder in sectors (32 bytes per entry, rounded up).
        root_secs = (self.root_entries * 32 + self.bps - 1) // self.bps
        # Cluster size in bytes.
        self.csize = self.bps * self.spc
        # The FAT starts right after the reserved sectors.
        self.fat_off = base + reserved * self.bps
        # FAT16's root folder follows all the FAT copies.
        self.root_off = base + (reserved + nfats * spf) * self.bps
        # The data area (cluster 2) follows the root folder.
        self.data_off = self.root_off + root_secs * self.bps
        # Number of data clusters.
        n = (total - (reserved + nfats * spf + root_secs)) // self.spc
        # The cluster count is what officially decides FAT12, FAT16 or FAT32.
        if n < 4085:
            raise ValueError("FAT12 not supported")
        # Clusters are numbered 2 .. n+1.
        self.max_cluster = n + 1
        # FAT32 has no fixed root folder.
        self.fat32 = self.root_entries == 0
        # FAT32 stores the root folder's first cluster at offset 44.
        self.root_cluster = u32(b, 44) if self.fat32 else 0    # FAT16 has no root cluster
        # Byte offset where this volume ends.
        self.end = base + total * self.bps
        # FAT entry size: 4 bytes on FAT32, 2 on FAT16.
        self.esz = 4 if self.fat32 else 2
        self.label = "FAT32" if self.fat32 else "FAT16"
        # Load the whole FAT into memory.
        self.fat = img.read_at(self.fat_off, spf * self.bps)

    # FAT entry for cluster c: the next cluster in the chain, 0 = free, a large value = end of chain.
    def _next(self, c):
        # Byte position of the entry.
        o = c * self.esz
        # Outside the table that was loaded.
        if o + self.esz > len(self.fat):
            return None
        # FAT32 uses only the low 28 bits of each entry.
        return (u32(self.fat, o) & 0x0FFFFFFF) if self.fat32 else u16(self.fat, o)

    # A zero entry means the cluster is unallocated.
    def is_free(self, c):
        return self._next(c) == 0

    # Follow a cluster chain from c and return the joined bytes (live folders).
    def read_chain(self, c, limit=64 << 20):
        # eoc: values at or above this mark the end of the chain.
        out, eoc = [], (0x0FFFFFF8 if self.fat32 else 0xFFF8)
        # The loop limit also stops cyclic chains.
        for _ in range(max(1, limit // self.csize)):
            # Invalid cluster number.
            if not 2 <= c <= self.max_cluster:
                break
            # Read this cluster, then follow the link.
            out.append(self.img.read_at(self.cluster_off(c), self.csize))
            c = self._next(c)
            # Unreadable, free, or end of chain.
            if c is None or c == 0 or c >= eoc:
                break
        return b"".join(out)

    # The root folder: an ordinary cluster chain on FAT32, a fixed area on FAT16.
    def _root(self):
        if self.fat32:
            return self.read_chain(self.root_cluster)
        return self.img.read_at(self.root_off, self.root_entries * 32)

    # Raw bytes of a sub-folder.
    def _read_dir(self, e):
        if not e.deleted:
            return self.read_chain(e.cluster)
        # Deleted folder: FAT chain is wiped, so assume it fit in one cluster.
        # The cluster has been reused by something else.
        if not self.is_free(e.cluster):
            return b""
        # A real folder starts with the '.' entry; use that to confirm it is still a folder.
        sub = self.img.read_at(self.cluster_off(e.cluster), self.csize)
        return sub if sub[:1] == b"." else b""

    # Checksum of the 11-byte short name. Long-name fragments store it, so they can be
    # matched to their short entry.
    @staticmethod
    def _lfn_sum(short):
        s = 0
        for ch in short:
            # Rotate right by one bit, add the byte, keep 8 bits.
            s = (((s & 1) << 7) + (s >> 1) + ch) & 0xFF
        return s

    # Decode 32-byte FAT directory entries into Entry objects.
    def _parse(self, data):
        # Long-name fragments collected for the next short entry.
        lfn = []
        # Step through whole 32-byte records.
        for i in range(0, len(data) - 31, 32):
            e = data[i:i + 32]
            # A first byte of 0 means there are no more entries.
            if e[0] == 0:
                break
            # Attribute pattern 0x0F marks a long-name fragment.
            if e[11] & 0x3F == 0x0F:                       # long-name fragment
                # Keep (checksum, 13 UTF-16 characters spread over three fields).
                lfn.append((e[13], e[1:11] + e[14:26] + e[28:32]))
                continue
            # Take the collected fragments and start a new list.
            parts, lfn = lfn, []
            # Attribute flags.
            attr = e[11]
            if attr & 0x08 or e[0] == 0x2E:                # volume label, . and ..
                continue
            # A first byte of 0xE5 marks a deleted entry.
            deleted = e[0] == 0xE5
            # 8.3 short name: 8 name bytes + 3 extension bytes.
            short = bytes(e[:11])
            # Name and extension, decoded with the old DOS code page.
            base_ = short[:8].decode("cp437", "replace").rstrip()
            ext = short[8:].decode("cp437", "replace").rstrip()
            # Deleting overwrote the first character with 0xE5, so it is unknown.
            if deleted:
                base_ = "_" + base_[1:]                    # first char is lost
            # Use the long name only if every fragment carries the same checksum and it matches this
            # entry (not checked for deleted entries, whose checksum may be stale).
            name = base_ + ("." + ext if ext else "")
            if parts and len({p[0] for p in parts}) == 1 and (
                    deleted or parts[0][0] == self._lfn_sum(short)):
                # Fragments are stored last-first, so reverse them.
                raw = b"".join(p[1] for p in reversed(parts))
                # Cut at the NUL terminator.
                long_name = raw.decode("utf-16le", "ignore").split("\x00")[0]
                name = long_name or name
            # First cluster: low 16 bits at offset 26, plus high 16 bits at offset 20 on FAT32.
            cl = u16(e, 26) | ((u16(e, 20) << 16) if self.fat32 else 0)
            # Size at offset 28; attribute 0x10 marks a folder.
            yield Entry(name, u32(e, 28), cl, deleted, bool(attr & 0x10))


# exFAT reader (most SD cards of 64 GB and up).
class ExFAT(Volume):
    label = "exFAT"

    def __init__(self, img, base):
        # exFAT boot sector.
        b = img.read_at(base, 512)
        # The filesystem name field reads 'EXFAT   '.
        if len(b) < 512 or b[3:11] != b"EXFAT   ":
            raise ValueError("not exFAT")
        self.img, self.base = img, base
        # Bytes per sector, stored as a power of two (a shift).
        bps = 1 << b[108]
        # Cluster size = sector size << the sectors-per-cluster shift.
        self.csize = bps << b[109]
        # FAT start (sector number -> bytes).
        self.fat_off = base + u32(b, 80) * bps
        # Start of the cluster heap (cluster 2).
        self.data_off = base + u32(b, 88) * bps
        # Cluster count + 1 = the highest cluster number.
        self.max_cluster = u32(b, 92) + 1
        # First cluster of the root folder.
        self.root_cluster = u32(b, 96)
        # Volume length.
        self.end = base + u64(b, 72) * bps
        # Load the FAT (4 bytes per entry).
        self.fat = img.read_at(self.fat_off, (self.max_cluster + 1) * 4)
        self.bitmap = b""
        # Read the root folder (up to 16 MiB).
        root = self.read_chain(self.root_cluster, 1 << 24)
        # The allocation bitmap (which clusters are in use) is described by a root entry of type 0x81.
        for i in range(0, len(root) - 31, 32):
            if root[i] == 0x81:                            # allocation bitmap
                # Read the bitmap: its first cluster is at offset 20, its length at offset 24.
                self.bitmap = img.read_at(self.cluster_off(u32(root, i + 20)),
                                          u64(root, i + 24))
                break
        # Without the bitmap, free clusters can't be told apart from used ones.
        if not self.bitmap:
            raise ValueError("exFAT bitmap not found")

    # Bit (c-2) of the bitmap: 0 = free.
    def is_free(self, c):
        if not 2 <= c <= self.max_cluster:
            return False
        # Which bitmap byte (8 clusters per byte).
        i = (c - 2) >> 3
        # (c-2) & 7 picks the bit inside that byte.
        return i < len(self.bitmap) and not (self.bitmap[i] >> ((c - 2) & 7)) & 1

    # Read `size` bytes starting at cluster c.
    def read_chain(self, c, size, nofat=False):
        # Contiguous data: no FAT lookup needed.
        if nofat:
            return self.img.read_at(self.cluster_off(c), size)
        out = []
        # Enough clusters to cover `size`.
        for _ in range(max(1, size // self.csize + 1)):
            if not 2 <= c <= self.max_cluster:
                break
            out.append(self.img.read_at(self.cluster_off(c), self.csize))
            # Next cluster from the FAT, or 0 if outside the loaded table.
            c = u32(self.fat, c * 4) if c * 4 + 4 <= len(self.fat) else 0
            # Free, or an end-of-chain / bad-cluster marker.
            if c == 0 or c >= 0xFFFFFFF7:
                break
        return b"".join(out)

    # Root folder.
    def _root(self):
        return self.read_chain(self.root_cluster, 1 << 24)

    # Raw bytes of a sub-folder, capped at 16 MiB.
    def _read_dir(self, e):
        size = min(e.size, 1 << 24)
        # Deleted folder: read it contiguously, but only if its cluster is still free.
        if e.deleted:
            return self.read_chain(e.cluster, size, True) if self.is_free(e.cluster) else b""
        return self.read_chain(e.cluster, size, e.nofat)

    # Decode exFAT 'entry sets': a file entry, then a stream entry, then name entries.
    def _parse(self, data):
        i = 0
        while i + 32 <= len(data):
            # Entry type byte.
            t = data[i]
            # 0 = end of the folder.
            if t == 0:
                break
            if t in (0x85, 0x05):                          # file entry set (live/deleted)
                # Number of secondary entries that follow.
                cnt = data[i + 1]
                # The whole set as one buffer.
                grp = data[i:i + 32 * (cnt + 1)]
                # Needs a stream entry (0xC0 live / 0x40 deleted) and at least one name entry.
                if cnt >= 2 and len(grp) == 32 * (cnt + 1) and grp[32] in (0xC0, 0x40):
                    # The stream-extension entry.
                    s = grp[32:64]
                    # Name length in characters.
                    nlen = s[3]
                    # Name entries carry 15 UTF-16 characters each, at bytes 2..31.
                    name = b"".join(grp[32 * k + 2:32 * k + 32] for k in range(2, cnt + 1))
                    name = name.decode("utf-16le", "ignore")[:nlen]
                    # Entry: name, data length, first cluster, deleted?, folder? (attribute 0x10),
                    # NoFatChain flag (bit 1 of the stream flags).
                    yield Entry(name, u64(s, 24), u32(s, 20), t == 0x05,
                                bool(u16(grp, 4) & 0x10), bool(s[1] & 0x02))
                    # Skip the whole set.
                    i += 32 * (cnt + 1)
                    continue
            # Any other entry: go to the next record.
            i += 32


# Locate FAT/exFAT volumes: on the whole disk first, then via the MBR or GPT partition table.
def find_volumes(img):
    # Try exFAT, then FAT, at byte offset `base`; None if neither fits.
    def attempt(base):
        for cls in (ExFAT, FAT):
            try:
                return cls(img, base)
            # Not this type, or damaged: try the next one.
            except Exception:
                pass
        return None

    # A card formatted without a partition table.
    v = attempt(0)
    if v:
        return [v]
    # Otherwise read the MBR (the first sector).
    vols, mbr = [], img.read_at(0, 512)
    if len(mbr) == 512 and mbr[510:512] == b"\x55\xaa":
        # Four 16-byte partition slots start at offset 446.
        for i in range(4):
            e = mbr[446 + 16 * i:462 + 16 * i]
            # Partition type byte and starting sector (LBA).
            ptype, lba = e[4], u32(e, 8)
            # Type 0xEE is a 'protective' MBR: the real table is GPT.
            if ptype == 0xEE:                              # GPT
                # The GPT header lives in sector 1.
                hdr = img.read_at(512, 92)
                # Missing or truncated header.
                if len(hdr) < 92 or hdr[:8] != b"EFI PART":
                    continue
                # Number of partition entries and the size of each.
                n, sz = u32(hdr, 80), u32(hdr, 84)
                # Sane limits; garbage values could otherwise mean billions of loop steps.
                if not (1 <= n <= 1024 and 128 <= sz <= 4096):     # corrupt header
                    continue
                # The partition entry array (its starting sector is at offset 72).
                ents = img.read_at(u64(hdr, 72) * 512, n * sz)
                for k in range(n):
                    ent = ents[k * sz:(k + 1) * sz]
                    # Unused slots have an all-zero type GUID.
                    if len(ent) >= 40 and any(ent[:16]):
                        # First sector (offset 32) -> byte offset.
                        v = attempt(u64(ent, 32) * 512)
                        if v:
                            vols.append(v)
            # An ordinary MBR partition that is in use.
            elif ptype and lba:
                v = attempt(lba * 512)
                if v:
                    vols.append(v)
    return vols


# ---------------------------------------------------------------------- carving
# The carving helpers below find where a photo starts and how long it is.
def classify(h):
    """Identify a photo header from the first 16 bytes of a sector."""
    # Switch on the first byte for speed.
    c = h[0]
    if c == 0xFF:
        # JPEG: FF D8 FF.
        return "jpeg" if h[:3] == b"\xff\xd8\xff" else None
    if c == 0x89:
        return "png" if h[:8] == PNG_SIG else None
    # TIFF-style RAW (CR2/NEF/ARW/DNG/...), Olympus ORF or Panasonic RW2.
    if c == 0x49:                                          # 'I'
        t = h[:4]
        return ("tiff" if t == b"II*\x00" else "orf" if t in (b"IIRO", b"IIRS")
                else "rw2" if t == b"IIU\x00" else None)
    # Big-endian TIFF.
    if c == 0x4D:
        return "tiff" if h[:4] == b"MM\x00*" else None
    # Fujifilm RAF.
    if c == 0x46:
        return "raf" if h[:8] == b"FUJIFILM" else None
    # Canon CR3: a 4-byte size, then 'ftypcrx '.
    if c == 0x00:
        return "cr3" if h[4:12] == b"ftypcrx " else None
    return None


# 0xFF followed by a real marker code. Excludes 00 (byte stuffing) and D0-D7 (restart
# markers), which appear inside the compressed picture data.
_MARKER = re.compile(rb"\xff[\x01-\xcf\xd8-\xfe]")         # a real JPEG marker


# JPEG length: walk the segments. EXIF blocks hold thumbnails with their own end marker,
# so simply searching for FF D9 would cut the file short.
def jpeg_end(img, start, limit):
    """Walk JPEG segments (skipping embedded thumbnails) to find the true EOI."""
    # Skip the FF D8 start marker.
    pos = start + 2
    while pos - start < limit:
        # Marker (2 bytes) + segment length (2 bytes).
        b = img.read_at(pos, 4)
        # Lost sync: not a valid marker.
        if len(b) < 2 or b[0] != 0xFF:
            return None
        m = b[1]
        # Padding FF bytes: slide by one.
        if m == 0xFF:
            pos += 1
        # Markers without a length field.
        elif m == 0x01 or 0xD0 <= m <= 0xD8:
            pos += 2
        # FF D9: end of image. The length includes the marker.
        elif m == 0xD9:
            return pos + 2 - start
        else:
            if len(b) < 4:
                return None
            # Big-endian segment length (it includes its own 2 bytes).
            ln = (b[2] << 8) | b[3]
            if ln < 2:
                return None
            # Jump over the marker and its payload.
            pos += 2 + ln
            # Start of scan: compressed data follows with no length field.
            if m == 0xDA:                                  # scan data until next marker
                p = pos
                while True:
                    if p - start >= limit:
                        return None
                    # Read 1 MiB of compressed data and find the next real marker.
                    blk = img.read_at(p, 1 << 20)
                    if len(blk) < 2:
                        return None
                    hit = _MARKER.search(blk)
                    if hit:
                        # Resume walking segments at that marker.
                        pos = p + hit.start()
                        break
                    # Overlap one byte so a marker split across two reads isn't missed.
                    p += len(blk) - 1
    return None


# PNG length: walk the chunks until IEND.
def png_end(img, start, limit):
    # The first chunk must be IHDR.
    if img.read_at(start + 12, 4) != b"IHDR":
        return None
    # Chunks start after the 8-byte signature.
    pos = start + 8
    while pos - start < limit:
        # Chunk header: 4-byte length + 4-byte type.
        h = img.read_at(pos, 8)
        if len(h) < 8:
            return None
        ln, typ = struct.unpack(">I4s", h)
        # PNG forbids lengths of 2^31 or more; chunk types are letters.
        if ln > 0x7FFFFFFF or not typ.isalpha():
            return None
        # length (4) + type (4) + data + CRC (4).
        pos += 12 + ln
        # The last chunk.
        if typ == b"IEND":
            return pos - start
    return None


# CR3 length: add up the top-level boxes.
def bmff_end(img, start, limit):
    """CR3 (ISO base media): sum top-level boxes. May exceed `limit`; measure() checks."""
    pos = start
    while pos - start < limit:
        h = img.read_at(pos, 16)
        if len(h) < 8:
            break
        # Box size and 4-character type.
        sz, typ = struct.unpack(">I4s", h[:8])
        # Types are printable ASCII.
        if not all(32 <= c < 127 for c in typ):
            break
        # Size 1 means a 64-bit size follows.
        if sz == 1 and len(h) == 16:
            sz = struct.unpack(">Q", h[8:16])[0]
        # 0 means 'to the end of the file'; below 8 is invalid.
        if sz < 8:
            break
        # Hop to the next box. A corrupt size can jump far past `limit`; measure() rejects that.
        pos += sz
    # Too short to be a photo -> None.
    return pos - start if pos - start > 4096 else None


# RAF length from the header.
def raf_len(img, start):
    """RAF: total length from the header's preview/raw offsets. measure() checks the limit."""
    h = img.read_at(start, 112)
    if len(h) < 108:
        return None
    # Offsets and lengths of the JPEG preview and the raw (CFA) data, big-endian.
    jo, jl, _, _, co, cl = struct.unpack(">6I", h[84:108])
    # The file ends where the later block ends.
    total = max(jo + jl, co + cl)
    return total if total > 4096 else None


# Extra check for TIFF headers, which also occur by chance.
def tiff_ok(img, start):
    h = img.read_at(start, 8)
    if len(h) < 8:
        return False
    # Bytes 4-7: offset of the first tag directory (IFD), in the file's own byte order.
    ifd = struct.unpack("<I" if h[:2] == b"II" else ">I", h[4:8])[0]
    # It must lie after the header and within 64 MiB.
    if not 8 <= ifd < (1 << 26):
        return False
    # The directory starts with a 2-byte entry count, which must be plausible.
    n = img.read_at(start + ifd, 2)
    return len(n) == 2 and 1 <= struct.unpack("<H" if h[:2] == b"II" else ">H", n)[0] <= 1000


# Length for TIFF-style RAW, ORF and RW2, which have no end marker.
def approx_end(img, start, limit):
    """Length for formats without a simple end marker: run to next header or zero padding.
    Returns (length, True if the data was still going when `limit` was reached)."""
    # Start scanning after the first sector.
    pos, last, zeros = start + SECTOR, start + SECTOR, 0
    while pos - start < limit:
        # Read up to 1 MiB, never past `limit`.
        blk = img.read_at(pos, min(1 << 20, start + limit - pos))
        if not blk:
            break
        # Look at each sector.
        for o in range(0, len(blk), SECTOR):
            sec = blk[o:o + SECTOR]
            # Another photo starts here: stop before it.
            if classify(sec[:16]):
                return last - start, False
            # An all-zero sector.
            if sec.count(0) == len(sec):
                zeros += 1
                # Eight blank sectors in a row = padding after the file.
                if zeros >= 8:
                    return last - start, False
            else:
                # A data sector: extend the file and reset the zero count.
                zeros, last = 0, pos + o + len(sec)
        pos += len(blk)
    # Ran out of data, or reached the limit (the second value True means 'cut off').
    return last - start, pos - start >= limit


# Picks the length finder for each kind and checks the result against the size caps.
def measure(img, kind, off):
    """Return (length, problem). length None = no usable file; problem says why, for the report."""
    # Bytes left on the card from here.
    room = img.size - off
    if kind == "jpeg":
        return jpeg_end(img, off, min(MAX_JPEG, room)), ""
    if kind == "png":
        return png_end(img, off, min(MAX_PNG, room)), ""
    # CR3 and RAF lengths come from headers, so check them before trusting them.
    if kind in ("cr3", "raf"):
        n = bmff_end(img, off, min(MAX_BMFF, room)) if kind == "cr3" else raf_len(img, off)
        lim = MAX_BMFF if kind == "cr3" else MAX_RAW
        # Runs past the end of the card: a damaged header or a cut-off file.
        if n and n > room:
            return None, "header says the file runs past the end of the card - damaged or cut off"
        # Bigger than the cap.
        if n and n > lim:
            return None, f"larger than the {lim >> 20} MiB size limit, or header damaged"
        return n, ""
    # TIFF headers need extra validation (many false hits).
    if kind == "tiff" and not tiff_ok(img, off):
        return None, ""
    # Everything else: an approximate length, flagged when cut at the cap.
    lim = min(MAX_RAW, room)
    n, full = approx_end(img, off, lim)
    return n, (f"cut at the {MAX_RAW >> 20} MiB size limit" if full and lim == MAX_RAW else "")


# File extension for a carved file.
def carved_ext(kind, data):
    if kind == "tiff":
        # Canon CR2 has 'CR' at bytes 8-9; other TIFF-based RAW is saved as .tif.
        return ".cr2" if data[8:10] == b"CR" else ".tif"   # NEF/ARW/DNG also land here
    return {"jpeg": ".jpg", "png": ".png", "raf": ".raf", "cr3": ".cr3",
            "orf": ".orf", "rw2": ".rw2"}[kind]


# Phase 1: do the first bytes at the file's old location match its extension?
# If not, something else has been written there.
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
    # Other types (--all-types): can't check.
    return True


# ---------------------------------------------------------------------- helpers
# Turn a path from the card into a safe path under `root`: replace illegal and control
# characters, strip leading/trailing spaces and dots (which also removes '.' and '..'),
# and use '_' for parts that end up empty.
def safe_path(root, rel):
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).strip(" .") or "_" for p in rel.split("/")]
    return root.joinpath(*parts)


# p, or p with _1, _2, ... added until the name is unused.
def unique(p):
    if not p.exists():
        return p
    i = 1
    while p.with_name(f"{p.stem}_{i}{p.suffix}").exists():
        i += 1
    return p.with_name(f"{p.stem}_{i}{p.suffix}")


# --verify support.
def verify_ok(path):
    """Decode the picture with Pillow if available; True when OK or unverifiable."""
    # Only JPEG and PNG are checked.
    if path.suffix.lower() not in (".jpg", ".png"):
        return True
    # Pillow is optional: without it, every file counts as OK.
    try:
        from PIL import Image as PILImage
    except ImportError:
        return True
    try:
        with PILImage.open(path) as im:
            # Force a full decode (open() alone only reads the header).
            im.load()
        return True
    # Any decode error means a broken file.
    except Exception:
        return False


# Used by both phases after saving a file.
def move_if_broken(dest, out, rel, a):
    """With --verify, move a picture Pillow can't decode into suspect/. Returns (path, ok)."""
    if not a.verify or verify_ok(dest):
        return dest, True
    # Same relative path under suspect/; create the folders first.
    new = unique(safe_path(out / "suspect", rel))
    new.parent.mkdir(parents=True, exist_ok=True)
    # Path.rename returns the new path.
    return dest.rename(new), False


# Copy n bytes from image offset `off` into a new file; returns its SHA-256.
def write_range(img, off, n, dest):
    # Create folders as needed.
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with open(dest, "wb") as f:
        done = 0
        while done < n:
            # Copy in 4 MiB pieces.
            buf = img.read_at(off + done, min(4 << 20, n - done))
            # Reached the end of the image early.
            if not buf:
                break
            # Hash while copying.
            h.update(buf)
            f.write(buf)
            done += len(buf)
    return h.hexdigest()


# Files created under sudo belong to root.
def give_back(paths):
    """Under sudo, hand files we created back to the real user (otherwise only root could delete them)."""
    # sudo records the real user's ids in these variables.
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    # Not running under sudo (or not on a Unix system): nothing to do.
    if not uid or not gid or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    for top in map(Path, paths):
        if not top.exists():
            continue
        # The path itself and, for folders, everything inside.
        for p in [top, *(top.rglob("*") if top.is_dir() else ())]:
            try:
                # lchown changes a symlink itself and never follows it.
                os.lchown(p, int(uid), int(gid))
            except OSError:
                pass


# Collects every per-file decision.
class Report:
    """report.csv (one row per file, written as we go so a crash keeps it) and
    summary.csv (three columns: recovered / failed / cannot recover), written at the end."""

    def __init__(self, out):
        self.out = out
        # report.csv is opened once and written row by row.
        self.f = open(out / "report.csv", "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(["result", "method", "original_name", "saved_as", "byte_offset",
                         "size", "detail", "sha256"])
        # Lists for the three summary.csv columns.
        self.lists = {OK: [], FAILED: [], CANNOT: []}

    # Record one decision.
    def add(self, result, method, original, saved, off, size, detail, digest=""):
        self.w.writerow([result, method, original, str(saved), off, size, detail, digest])
        # Flush to disk so the row survives a crash or Ctrl-C.
        self.f.flush()
        name = str(saved or original)
        # summary.csv shows the saved path (or the card path) and, unless recovered, the reason.
        self.lists[result].append(name if result == OK else f"{name} ({detail})")

    # Called at the end of the run, also after Ctrl-C or an error.
    def close(self):
        self.f.close()
        with open(self.out / "summary.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # Header row: the three category names.
            w.writerow(list(self.lists))
            # zip_longest pads the shorter columns with empty cells.
            w.writerows(zip_longest(*self.lists.values(), fillvalue=""))


# ---------------------------------------------------------------------- phases
# PHASE 1: restore deleted files found in the folder tree.
def recover_fs(vol, img, out, a, hashes, report):
    for e in vol.walk():
        # Only deleted, non-empty files.
        if not e.deleted or e.size == 0:
            continue
        ext = Path(e.name).suffix.lower()
        # Photos only, unless --all-types.
        if not a.all_types and ext not in PHOTO_EXTS:
            continue
        # Clusters needed (ceiling division).
        need = -(-e.size // vol.csize)
        first = e.cluster
        # The file must fit inside the volume.
        if first < 2 or first + need - 1 > vol.max_cluster:
            report.add(CANNOT, "filesystem", e.path, "", "", e.size, "directory entry damaged - bad cluster range")
            continue
        # The first cluster has been reused by another file.
        if not vol.is_free(first):
            report.add(CANNOT, "filesystem", e.path, "", "", e.size, "overwritten by newer data")
            continue
        # Byte position of the data.
        off = vol.cluster_off(first)
        # The header must match the extension.
        if not sig_ok(ext, img.read_at(off, 16)):
            report.add(CANNOT, "filesystem", e.path, "", off, e.size, "overwritten - data at its location is something else")
            continue
        # How many of the file's clusters are still unallocated (not overwritten).
        free = sum(vol.is_free(c) for c in range(first, first + need))
        # Keep the card's folder structure under filesystem/.
        dest = unique(safe_path(out / "filesystem", e.path))
        # Save (a contiguous copy of e.size bytes), then optionally verify.
        try:
            digest = write_range(img, off, e.size, dest)
            dest, ok = move_if_broken(dest, out, e.path, a)
        except OSError as ex:
            # Disk full: stop the whole run (main() explains it).
            if ex.errno in DISK_FULL:
                raise
            # Any other save error: record it and carry on with the next file.
            report.add(FAILED, "filesystem", e.path, "", off, e.size, f"could not save: {ex}")
            continue
        # Remember the fingerprint so carving skips this file.
        hashes.add(digest)
        # Decide the result category.
        if not ok:
            result, detail = FAILED, "saved to suspect/: picture does not decode"
        elif free != need:
            result, detail = FAILED, "saved, but partly overwritten - probably corrupt"
        else:
            result, detail = OK, "intact"
        report.add(result, "filesystem", e.path, dest.relative_to(out), off, e.size, detail, digest)


# PHASE 2: scan the card for photo headers and cut the files out.
def carve(img, vols, out, a, hashes, report):
    # Inside a known volume, only carve at the start of free clusters, so live files aren't
    # copied again. Outside any volume (or with no volume found), everything is allowed.
    def allowed(off):
        for v in vols:
            if v.base <= off < v.end:
                return v.start_free(off)
        return True

    # found: carved count for the progress line; resume: skip offsets inside the last carved file.
    found, resume, total = 0, 0, img.size
    # 8 MiB at a time.
    for base in range(0, total, CHUNK):
        # +16 bytes so a header at the very end of the chunk is complete.
        chunk = img.read_at(base, CHUNK + 16)
        # Photo headers are sector-aligned.
        for o in range(0, min(CHUNK, total - base), SECTOR):
            # First 16 bytes of this sector.
            h = chunk[o:o + 16]
            if not h:
                break
            kind = classify(h)
            # Absolute offset on the card.
            off = base + o
            # Not a header, inside the previous file, or in an allocated cluster.
            if not kind or off < resume or not allowed(off):
                continue
            # How long is the file, and is anything wrong with it?
            length, problem = measure(img, kind, off)
            if not length:
                # Report real photo headers that can't be used; ignore plain false hits.
                if problem:
                    report.add(CANNOT, "carving", f"{kind} at byte {off:,}", "", off, "", problem)
                continue
            # Too small: usually an embedded thumbnail.
            if length < a.min_size:
                continue
            # The whole file into memory, then fingerprint it.
            data = img.read_at(off, length)
            digest = hashlib.sha256(data).hexdigest()
            # Continue scanning after this file.
            resume = off + length
            # Already recovered (in Phase 1 or earlier in Phase 2).
            if digest in hashes:
                continue
            hashes.add(digest)
            # Named by hex offset on the card, e.g. f_0000c4a20000.raf.
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
            # Decide the result category.
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
        # Progress line, rewritten in place (carriage return) on stderr.
        print(f"\r  carving: {min(base + CHUNK, total) * 100 // total:3d}%  "
              f"({found} found)", end="", file=sys.stderr, flush=True)
    # Final newline after the progress line.
    print(file=sys.stderr)


# Runs both phases on an Image and writes the reports. Used by scan and recover.
def run_recovery(img, out, a):
    out.mkdir(parents=True, exist_ok=True)
    try:
        # Find FAT/exFAT volumes; a damaged partition table just means carving only.
        vols = find_volumes(img)
    except Exception as ex:                                # damaged partition table
        print(f"warning: partition table unreadable ({ex})")
        vols = []
    if vols:
        for v in vols:
            print(f"Found {v.label} volume at byte {v.base:,} (cluster {v.csize:,} B)")
    else:
        print("No readable FAT/exFAT volume (formatted or damaged?) - carving only.")
    # The report, and the SHA-256 fingerprints shared by both phases.
    report, hashes = Report(out), set()
    try:
        for v in vols:
            print(f"Phase 1: scanning {v.label} for deleted files...")
            try:
                recover_fs(v, img, out, a, hashes, report)
            # A broken folder structure ends Phase 1 for this volume but not the run;
            # disk full is re-raised so main() can explain it.
            except Exception as ex:                        # damaged directory structures
                if isinstance(ex, OSError) and ex.errno in DISK_FULL:
                    raise
                print(f"  warning: directory scan stopped early ({ex})")
        if not a.no_carve:
            print("Phase 2: carving free space for photo signatures...")
            carve(img, vols, out, a, hashes, report)
    # Always runs, even after Ctrl-C or an error: write summary.csv, give the files back to
    # the user and print the totals.
    finally:
        report.close()
        give_back([out / n for n in ("filesystem", "carved", "suspect", "report.csv", "summary.csv")])
        n = {k: len(v) for k, v in report.lists.items()}
        print(f"\n{OK}: {n[OK]}   {FAILED}: {n[FAILED]}   {CANNOT}: {n[CANNOT]}")
        print(f"Files:   {out.resolve()}/")
        print(f"Details: {out / 'report.csv'} and {out / 'summary.csv'}")


# ------------------------------------------------------------------ macOS disks
# macOS only from here: find disks with Apple's 'diskutil' tool.
# Runs 'diskutil <verb> -plist ...' and parses the output into Python dicts and lists.
def diskutil(verb, *args):
    try:
        r = subprocess.run(["diskutil", verb, "-plist", *args], capture_output=True, check=True)
    except FileNotFoundError:
        raise UserError("diskutil not found: choosing a card by name works on macOS only") from None
    except subprocess.CalledProcessError:
        raise UserError(f"diskutil could not find {' '.join(args) or verb}") from None
    return plistlib.loads(r.stdout)


# One whole disk, as diskutil describes it.
@dataclass
class Disk:
    ident: str                                             # whole-disk id, e.g. disk6
    # size: true size in bytes (a raw device reports 0 itself)
    size: int
    # block: device block size; raw reads must be multiples of it
    block: int
    # bus: e.g. 'Secure Digital', 'USB', 'Disk Image'
    bus: str
    own: bool                                              # holds this Mac's system: never read
    # names: volume names on the disk; mounts: where they are mounted
    names: list = field(default_factory=list)
    mounts: list = field(default_factory=list)


# Builds the list scan shows, and the safety check behind every device path.
def list_disks():
    """Every whole disk macOS knows about, with the Mac's own disks flagged."""
    # The disk holding the running macOS ('/') ...
    boot = diskutil("info", "/")
    system = {boot.get("ParentWholeDisk")}
    # ... and the physical disk under its APFS container (e.g. disk0).
    system |= {diskutil("info", s["APFSPhysicalStore"]).get("ParentWholeDisk")
               for s in boot.get("APFSPhysicalStores", [])}
    disks = []
    # Every disk, with its partitions and APFS volumes.
    for d in diskutil("list")["AllDisksAndPartitions"]:
        info = diskutil("info", d["DeviceIdentifier"])
        parts = [d] + d.get("Partitions", []) + d.get("APFSVolumes", [])
        # Built-in SD slots report "internal" too, so only internal *fixed* disks count as the Mac's own.
        own = d["DeviceIdentifier"] in system or bool(info.get("Internal") and not info.get("RemovableMedia"))
        # Size from diskutil (raw devices report 0), block size, bus, names and mount points.
        disks.append(Disk(d["DeviceIdentifier"], info.get("Size") or info.get("TotalSize") or 0,
                          info.get("DeviceBlockSize") or SECTOR, info.get("BusProtocol", "?"), own,
                          [p["VolumeName"] for p in parts if p.get("VolumeName")],
                          [p["MountPoint"] for p in parts if p.get("MountPoint")]))
    return disks


# Used by the image and recover commands when given a /dev path on macOS.
def mac_device(path):
    """(size, block size, whole Disk) for /dev/[r]diskN[sM]; refuses the Mac's own disks."""
    # Accept /dev/diskN, /dev/rdiskN and partitions such as /dev/rdisk4s1.
    m = re.fullmatch(r"/dev/r?(disk\d+(?:s\d+)*)", path)
    if not m:
        raise UserError(f"{path}: expected a disk device such as /dev/rdisk4")
    info = diskutil("info", m.group(1))
    # Find the whole disk it belongs to; refuse the Mac's own disks.
    disk = next((d for d in list_disks() if d.ident == info.get("ParentWholeDisk")), None)
    if disk is None or disk.own:
        raise UserError(f"{path} is one of this Mac's own disks - refusing to read it")
    return info.get("Size") or info.get("TotalSize") or 0, info.get("DeviceBlockSize") or SECTOR, disk


def refuse_on_card(path, disk):
    """Never write the image or recovered files onto the card being recovered."""
    # Resolve to an absolute path, then refuse it if it is a mount point of the card or inside one.
    p = Path(path).expanduser().resolve()
    for m in map(Path, disk.mounts):
        if p == m or m in p.parents:
            raise UserError(f"{path} is on the card itself - choose a folder on your Mac "
                            "(writing to the card destroys deleted files)")


# True for character or block devices such as /dev/rdisk4 or /dev/sdb.
def is_device(path):
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


# Used by the recover command.
def open_source(path, out):
    """Open an image file, or a device (with its real size and block alignment)."""
    if not is_device(path):
        img = Image(path)
        if not img.size:
            raise UserError(f"{path} is empty")
        return img
    # macOS device: real size and block size from diskutil; the output must not be on the card.
    if sys.platform == "darwin":
        size, block, disk = mac_device(path)
        refuse_on_card(out, disk)
        return Image(path, size, block)
    # Linux block devices report their size and accept any offset.
    img = Image(path)
    if not img.size:
        raise UserError(f"can't tell the size of {path} - copy it with the image command first")
    return img


# Used by scan (copy before recovering) and by the image command.
# Mode 'xb' creates a new file and fails if one exists, so nothing is ever overwritten.
def copy_device(src_path, dest, total, block=SECTOR, mode="xb"):
    """Sector-by-sector copy; unreadable sectors become zeros. Returns (bytes copied, bad sectors)."""
    bad, off = 0, 0
    with open(src_path, "rb", buffering=0) as src, open(dest, mode) as d:
        # Until the known size is reached (or, if unknown, until the device stops returning data).
        while not total or off < total:
            # 1 MiB at a time, smaller for the last piece.
            n = min(1 << 20, total - off) if total else 1 << 20
            try:
                src.seek(off)
                buf = src.read(n)
            except OSError:                                # unreadable area: go sector by sector
                buf, failed = b"", 0
                # Retry each block on its own; unreadable ones become zeros and are counted.
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
            # End of the device.
            if not buf:
                break
            d.write(buf)
            off += len(buf)
            # Progress as a percentage when the size is known, otherwise MiB copied.
            pct = f"{off * 100 // total:3d}%" if total else f"{off >> 20} MiB"
            print(f"\r  copying: {pct}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return off, bad


# -------------------------------------------------------------------- commands
# 'scan' command (the default): pick the card by name, copy it, recover from the copy.
def cmd_scan(a):
    if sys.platform != "darwin":
        raise UserError("choosing a card by name needs macOS - use the image and recover commands instead")
    # Never offer the Mac's own disks; hide disk images unless asked.
    disks = [d for d in list_disks() if not d.own and (a.allow_disk_images or d.bus != "Disk Image")]
    if not disks:
        raise UserError("no SD card or external disk found - insert the card and try again")
    print("Cards and external disks (your Mac's own disks are never listed or read):")
    # Show the candidates: names, disk id, size in decimal GB (as Finder shows it), bus.
    for d in disks:
        print(f"  {', '.join(d.names) or '(no name)':<28} {d.ident:<8} {d.size / 1e9:7.1f} GB  {d.bus}")
    # --volume skips the question.
    name = a.volume or input("\nName of the card to scan (or its disk id, e.g. disk4): ").strip()
    # Match a volume name (any case) or a disk id.
    hits = [d for d in disks
            if name.casefold() in (n.casefold() for n in d.names) or name == d.ident]
    if not hits:
        raise UserError(f"'{name}' is not one of the disks listed above")
    if len(hits) > 1:
        raise UserError(f"several disks have a volume called '{name}' - type the disk id instead "
                        f"({', '.join(d.ident for d in hits)})")
    # Read the whole disk (all partitions) through the faster raw device.
    disk, dev = hits[0], f"/dev/r{hits[0].ident}"
    out = Path(a.output).expanduser().resolve()
    # Refuse an output folder on the card itself.
    refuse_on_card(out, disk)
    print(f"\nThe whole card ({disk.ident}, {disk.size / 1e9:.1f} GB) will be read. Results go to {out}/")
    # Confirm before reading (skipped with --volume).
    if not a.volume and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled - nothing was read.")
        return
    # Reading a real card needs sudo; say so clearly.
    if not os.access(dev, os.R_OK):
        raise UserError(f"no permission to read {dev} - run it again with sudo")
    # Remember whether we created the output folder, so only then is it handed back.
    created = not out.exists()
    out.mkdir(parents=True, exist_ok=True)
    try:
        # Copy (or open directly), then run both phases.
        img, copy = card_source(disk, dev, out, a)
        run_recovery(img, out, a)
        if copy:
            # Tell the user how to re-scan the copy; shlex.quote makes paths with spaces paste correctly.
            print(f"\nThe card copy is {copy}. To scan it again without touching the card:\n"
                  f"  python3 {shlex.quote(sys.argv[0])} recover {shlex.quote(str(copy))} -o {shlex.quote(str(out) + '_2')}\n"
                  "Delete the copy when you're done; it is as big as the card.")
    finally:
        # Under sudo, give the output folder back to the user.
        give_back([out] if created else [])


# Decides between copying the card first and reading it directly.
def card_source(disk, dev, out, a):
    """Copy the card into `out` when there's room (one pass over the card, repeatable);
    otherwise read the card directly. Returns (Image, path of the copy or None)."""
    if not a.direct:
        # Name the copy after the card, keeping only safe characters.
        label = re.sub(r"[^\w.-]", "_", disk.names[0] if disk.names else disk.ident)
        copy = unique(out / f"{label}.img")
        # Copy only when there is room for the whole card plus 1 GiB.
        free = shutil.disk_usage(out).free
        if free >= disk.size + (1 << 30):
            # Copy into a .part file and rename it when complete, so an interrupted copy is obvious.
            part = copy.with_name(copy.name + ".part")
            print(f"Copying the card to {copy} ...")
            try:
                done, bad = copy_device(dev, part, disk.size, disk.block, "wb")
            finally:
                give_back([part])
            # A short copy: the card or reader stopped responding.
            if done < disk.size:
                raise UserError(f"the card stopped responding after {done:,} bytes - partial copy kept in {part}")
            part.rename(copy)
            print(f"Copied {done:,} bytes" + (f" ({bad} unreadable sectors zero-filled)" if bad else ""))
            return Image(copy), copy
        # Not enough room: read the card directly instead.
        print(f"Not enough free space to copy the card first ({disk.size / 1e9:.1f} GB needed, "
              f"{free / 1e9:.1f} GB free) - reading the card directly.")
    return Image(dev, disk.size, disk.block), None


# 'image' command: sector-by-sector copy of a device into a new file.
def cmd_image(a):
    dest = Path(a.dest).expanduser()
    # Never overwrite an existing file, and never write into /dev.
    if dest.exists() or dest.resolve().parts[1:2] == ("dev",):
        raise UserError(f"{dest} already exists - choose a new file name (nothing was written)")
    total, block = 0, SECTOR
    # macOS device: real size, block size and safety checks via diskutil.
    if is_device(a.source) and sys.platform == "darwin":
        total, block, disk = mac_device(a.source)
        refuse_on_card(dest, disk)
    else:
        # Other sources: ask the OS for the size.
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
    # The source ended early (card removed, reader error).
    if total and done < total:
        print(f"warning: the source stopped responding after {done:,} of {total:,} bytes - the copy is incomplete")


# 'recover' command: run both phases on an image file (or a device).
def cmd_recover(a):
    out = Path(a.output).expanduser()
    run_recovery(open_source(a.image, out), out, a)


# Command-line interface.
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
    # 'scan' and its own options.
    s = sub.add_parser("scan", parents=[opts],
                       help="macOS: pick the card by name, then copy and recover it (the default)")
    s.add_argument("--volume", help="the card's name as shown in Finder (skips the questions)")
    s.add_argument("--direct", action="store_true", help="read the card directly instead of copying it first")
    s.add_argument("--allow-disk-images", action="store_true", help="also offer mounted disk images (.dmg)")
    s.set_defaults(fn=cmd_scan)
    # 'image' and its arguments.
    i = sub.add_parser("image", help="make a sector-by-sector copy of the card")
    i.add_argument("source", help="device, e.g. /dev/rdisk4 or /dev/sdb")
    i.add_argument("dest", help="new .img file (needs free space = card size)")
    i.set_defaults(fn=cmd_image)
    # 'recover' and its arguments.
    r = sub.add_parser("recover", parents=[opts], help="recover files from an image")
    r.add_argument("image", help="image file made by the image command (a device path also works)")
    r.set_defaults(fn=cmd_recover)
    # No command given, or only options: default to scan.
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("scan", "image", "recover", "-h", "--help"):
        argv = ["scan", *argv]                            # no command given: pick the card by name
    a = p.parse_args(argv)
    # Run the command, turning known problems into one-line messages instead of tracebacks.
    # sys.exit('message') prints the message to stderr and exits with code 1.
    try:
        a.fn(a)
    except UserError as ex:
        sys.exit(f"error: {ex}")
    except KeyboardInterrupt:
        sys.exit("\nStopped. Everything saved so far is kept.")
    except EOFError:
        sys.exit("\nerror: no answer given")
    # Usually a missing sudo, or macOS privacy settings.
    except PermissionError as ex:
        sys.exit(f"error: {ex}\nReading a card needs administrator rights: run it again with sudo. If you already "
                 "did, give your terminal app Full Disk Access (System Settings > Privacy & Security).")
    # Disk full gets a clear message; other OS errors are printed as they are.
    except OSError as ex:
        if ex.errno in DISK_FULL:
            sys.exit("error: no space left where the files are being saved - free some space and run again (files saved so far are kept)")
        sys.exit(f"error: {ex}")


# Run main() only when executed directly, not when imported (e.g. by tests).
if __name__ == "__main__":
    main()
