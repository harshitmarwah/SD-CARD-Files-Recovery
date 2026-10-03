# On-disk formats used by the recovery tool

All FAT/exFAT/MBR/GPT integers are little-endian. JPEG, PNG and the RAF/CR3 headers are big-endian. TIFF uses the byte order in its first two bytes.

## Contents
1. Partition tables (MBR, GPT)
2. FAT16 / FAT32
3. exFAT
4. What deletion changes
5. Photo signatures and how to measure length

---

## 1. Partition tables

**MBR** is sector 0 and ends with `55 AA` at bytes 510–511.

| Offset | Size | Field |
|---|---|---|
| 446 + 16·i | 16 | Partition slot i (i = 0..3) |
| slot + 4 | 1 | Type (0x0B/0x0C FAT32, 0x06/0x0E FAT16, 0x07 exFAT, **0xEE = protective MBR, read GPT**) |
| slot + 8 | 4 | First sector (LBA) |

**GPT header** is at sector 1 (byte 512).

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | `"EFI PART"` |
| 72 | 8 | First sector of the entry array |
| 80 | 4 | Number of entries (normally 128; **reject outside 1–1024**) |
| 84 | 4 | Size of each entry (normally 128; **reject outside 128–4096**) |

Each GPT entry has its type GUID at bytes 0–15 (all zeros means unused) and its first LBA at 32 (8 bytes).

A card may also have **no partition table**, with the filesystem starting at byte 0. Try that first.

## 2. FAT16 / FAT32

**Boot sector (BPB):**

| Offset | Size | Field |
|---|---|---|
| 11 | 2 | Bytes per sector (512/1024/2048/4096 only) |
| 13 | 1 | Sectors per cluster (power of two) |
| 14 | 2 | Reserved sectors (> 0) |
| 16 | 1 | Number of FATs (1 or 2) |
| 17 | 2 | Root entries (FAT16; 0 on FAT32) |
| 19 / 32 | 2 / 4 | Total sectors (16-bit, else 32-bit) |
| 22 / 36 | 2 / 4 | Sectors per FAT (16-bit, else FAT32's 32-bit) |
| 44 | 4 | Root folder's first cluster (**FAT32 only**; garbage on FAT16) |
| 48 | 2 | FSInfo sector (FAT32). The free-cluster count is at FSInfo + 488, and macOS rewrites it on unmount. |
| 510 | 2 | `55 AA` |

**Layout:** reserved sectors → FAT copies → (FAT16) fixed root folder → data area. Cluster 2 is the first cluster of the data area.

**Type by cluster count:** under 4085 is FAT12 (not supported), under 65525 is FAT16, anything higher is FAT32.

**FAT entries:**
- FAT16 uses 2-byte entries; values 0xFFF8 and above mark the end of a chain.
- FAT32 uses 4-byte entries, of which only the low 28 bits count; 0x0FFFFFF8 and above mark the end of a chain.
- 0 means the cluster is free.

**Directory entry (32 bytes):**

| Offset | Field |
|---|---|
| 0–10 | 8.3 name (first byte: `00` = end of folder, `E5` = deleted, `2E` = `.`/`..`) |
| 11 | Attributes (0x08 volume label, 0x10 folder, **0x0F = long-name fragment**) |
| 20 | High 16 bits of the first cluster (FAT32) |
| 26 | Low 16 bits of the first cluster |
| 28 | Size in bytes |

**Long-name (LFN) fragment:**
- Layout:
  - byte 0: sequence number;
  - 1–10: characters 1–5 (UTF-16);
  - 11: 0x0F;
  - 13: checksum of the 8.3 name;
  - 14–25: characters 6–11;
  - 28–31: characters 12–13.
- Fragments come before their short entry, stored last-first.
- Checksum: `s = ((s & 1) << 7) + (s >> 1) + byte` over the 11 name bytes, kept to 8 bits.

## 3. exFAT

**Boot sector:**

| Offset | Size | Field |
|---|---|---|
| 3 | 8 | `"EXFAT   "` |
| 72 | 8 | Volume length (sectors) |
| 80 | 4 | FAT offset (sectors) |
| 88 | 4 | Cluster heap offset (sectors), where cluster 2 sits |
| 92 | 4 | Cluster count |
| 96 | 4 | Root folder's first cluster |
| 108 | 1 | Bytes-per-sector shift (sector = 1 << value) |
| 109 | 1 | Sectors-per-cluster shift |

**Directory entries** are 32 bytes. The first byte is the type, and bit 0x80 means "in use", so deleting clears it.

| Type | Meaning | Fields |
|---|---|---|
| 0x81 | Allocation bitmap | first cluster @20, length @24 (bit = 1 means used) |
| 0x85 / **0x05** | File (live / deleted) | secondary count @1, attributes @4 (0x10 folder) |
| 0xC0 / **0x40** | Stream extension | flags @1 (bit 1 = NoFatChain, i.e. contiguous), name length @3, first cluster @20, data length @24 |
| 0xC1 / 0x41 | File name | 15 UTF-16 characters @2 |

A file is an *entry set*: one 0x85 entry, one 0xC0 entry, then name entries.

## 4. What deletion changes

| | FAT16/32 | exFAT |
|---|---|---|
| Directory entry | First byte → 0xE5 (first letter of the 8.3 name lost; the long name may survive) | Type bit 0x80 cleared; **full name kept** |
| Allocation | FAT chain zeroed, so assume the file is contiguous | Bitmap bits cleared; files are usually contiguous (NoFatChain) |
| Data | Untouched until overwritten | Untouched until overwritten |

Recovery test for one entry:
1. The first cluster is in range.
2. The first cluster is still free.
3. The signature at that cluster matches the extension.

Then copy `size` bytes. If any later cluster is no longer free, flag the file as "partly overwritten".

## 5. Photo signatures and length

Check headers at the start of a sector (carving only at free-cluster starts).

| Format | Signature | How to find the length |
|---|---|---|
| JPEG | `FF D8 FF` | Walk the segments: `FF xx` + 2-byte BE length (which includes itself). Markers without a length: `D0`–`D8`, `01`. On `DA` (start of scan), search for the next real marker `FF [01-CF D8-FE]`; `FF 00` is byte stuffing. Stop at `FF D9`. Don't search blindly for `FF D9`, because EXIF thumbnails contain their own. |
| PNG | `89 50 4E 47 0D 0A 1A 0A`, then `IHDR` at 12 | Chunks: 4-byte BE length + 4-letter type + data + 4-byte CRC, until `IEND`. Reject lengths ≥ 2^31 and non-letter types. |
| TIFF / CR2 / NEF / ARW / DNG | `II*\0` or `MM\0*` (CR2: `CR` at 8) | No end marker. Validate the first IFD offset (bytes 4–7, 8 ≤ x < 64 MiB) and its entry count (1–1000). Then estimate: run until the next header or 8 zero sectors, capped. |
| ORF | `IIRO` / `IIRS` | As TIFF (estimate) |
| RW2 | `IIU\0` | As TIFF (estimate) |
| RAF | `FUJIFILM` | Six BE u32s at 84: JPEG offset, JPEG length, meta offset, meta length, CFA offset, CFA length. Length = max(JPEG end, CFA end). **Reject if past the card's end or over the cap.** |
| CR3 | bytes 4–11 = `ftypcrx ` | ISO-BMFF: add up the top-level boxes (BE u32 size + 4-char type; size 1 means a 64-bit size follows; under 8 is invalid). **The sum can overshoot wildly; reject if past the card or the cap.** |

The minimum carved size (10 KiB) skips embedded thumbnails. SHA-256 de-duplicates between Phase 1 and Phase 2.
