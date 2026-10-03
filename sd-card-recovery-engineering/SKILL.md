---
name: sd-card-recovery-engineering
description: How to build, extend, debug or harden a read-only SD card / disk photo-recovery tool like sd_recover.py - parsing MBR/GPT and FAT16/FAT32/exFAT on-disk structures, restoring deleted directory entries, signature-carving JPEG/PNG/RAW files, reading raw devices on macOS, picking a card by name - and the defensive techniques that stop such tools from crashing, hanging, exhausting memory or writing to the card. Use this skill whenever someone writes or reviews code that reads raw disks or disk images, parses binary filesystem or file-format headers, carves or undeletes files, selects disks with diskutil, or asks why a recovery/forensics script crashes, hangs or runs out of memory on a damaged card - even if they never say "recovery" or "robust".
---

# SD card recovery engineering

What we learned building `sd_recover.py`: a single-file, standard-library Python 3.9+ tool that recovers deleted photos from SD cards without ever writing to them. The first half of this file describes how the tool is put together. The second half, the robustness playbook, is the part that matters most. Every rule in it exists because breaking it crashed, hung or silently lost photos in a real reproduction.

## Two rules everything else follows

1. **The card is evidence.** A deleted photo survives only until something overwrites its clusters. So the tool opens every source read-only, and refuses to put output on the card or anywhere it could land on the card. It also has to remember that the operating system itself writes to mounted cards.
2. **Every byte read from the card is hostile input.** Damaged or reused sectors produce sizes like 1 TiB, partition tables with four billion entries, and folders that contain themselves. A length, offset or count read from disk is a claim to check, never a fact to use.

## Architecture

```
pick device (macOS: by volume name via diskutil; else a /dev path)
  -> copy the whole card to an image if there is room, else read the device directly
  -> find volumes: whole-disk FAT/exFAT first, then MBR / GPT partitions
  -> Phase 1 (filesystem): walk folders incl. deleted ones, restore deleted entries
  -> Phase 2 (carving): scan free clusters for photo signatures, measure, cut out
  -> report.csv (row by row) + summary.csv (Recovered / Failed / Cannot recover)
```

| Piece | Job | Key idea |
|---|---|---|
| `Image` | The only way anything reads the source | `read_at(off, n)`: clamped, block-aligned for devices, zero-fills bad blocks |
| `Volume` / `FAT` / `ExFAT` | Parse the filesystem; `walk()` yields entries | Shared walk with a visited-set; subclasses supply `_parse`, `_read_dir`, `is_free` |
| `find_volumes` | Locate volumes | Try byte 0, then MBR slots, then GPT if the MBR is protective (0xEE) |
| `recover_fs` | Phase 1 | Deleted entry + first cluster still free + signature still matches → copy contiguous run |
| `carve` + `measure` | Phase 2 | Headers only at free-cluster starts; length from file structure, never by guessing past caps |
| `Report` | Results | Every decision is a row; three categories; written as you go |
| `list_disks` / `mac_device` / `refuse_on_card` | macOS safety | Never read the Mac's own disks, never write onto the card |
| `copy_device` | Imaging | Exclusive create, loop to known size, sector fallback, `.part` then rename |

Phase 1 relies on how deletion works. FAT zeroes the file's cluster chain, and exFAT clears the in-use bits, but the directory entry (name, size, first cluster) and the data stay put. Cameras write files contiguously, so "first cluster + size" is enough to recover a file. Phase 2 catches what Phase 1 can't, such as entries that were reused or wiped by a quick format. Duplicates between the two phases are skipped by SHA-256. Byte layouts for all of this are in `references/formats.md`.

## Robustness playbook

Each rule has a reason and the failure we reproduced without it. When reviewing or extending such a tool, walk through these.

### A. Never make recovery worse

- **Open sources read-only.** Create images with exclusive create (`open(dest, "xb")`), refuse destinations under `/dev`, and refuse paths that already exist. *Without it:* swapping the two arguments of `image` would copy an old image onto the card.
- **Refuse output on the card.** Resolve the output path and compare it against every mount point of the source disk. Writing the image or the recovered files to the card overwrites the very clusters being recovered.
- **Tell the user to unmount before scanning.** Raw reads still work on an unmounted card. *Observed on macOS:*
  - mounting created `.fseventsd`;
  - copying created `._*` sidecar files;
  - Spotlight built `.Spotlight-V100` on the user's card;
  - merely unmounting a FAT32 image rewrote the FSInfo free-cluster count.
- **Don't browse a mounted card while investigating.** Directory reads can update access times on FAT/exFAT. Use `stat` on single paths, or better, the raw device.

### B. Never trust a number read from disk

- **Clamp every read at the I/O layer.** Use `n = min(n, size - off)` before allocating or seeking. A single choke point protects all parsers. Huge offsets become empty reads instead of `OverflowError` or `MemoryError`.
- **Check buffer lengths before `struct.unpack`.** *Without it:* a GPT header cut to 78 bytes raised `struct.error` and crashed the run before carving started.
- **Give structural fields sane ranges.**
  - GPT: 1–1024 entries of 128–4096 bytes. *Without it:* absurd values gave `OverflowError` or a near-endless loop.
  - FAT: legal sector size, cluster size a power of two, 1–2 FATs.
- **Validate lengths taken from file headers before using them.** *Without it:* a CR3 box claiming 1 TiB was reported as an "exact" 1 TiB file. Worse, the scan's resume pointer jumped past the next real JPEG, so that photo was lost. Reject lengths that run past the card or exceed the cap, and only advance the resume pointer for files actually accepted.

### C. Never loop without a bound

- **Track visited folder clusters.** A depth limit alone isn't enough. *Without the visited set:* four folder entries pointing back at the root meant 4^16 ≈ 4.3 billion visits, a hang. With it, the same image finished in 0.04 s. Seed the set with the root cluster.
- **Bound cluster-chain walks by the expected size.** `size // cluster + 1` iterations stops cyclic FAT chains.
- **Make every scan loop provably advance.** When searching a block for a marker, overlap by one byte (`p += len(blk) - 1`) so a marker split across blocks isn't missed, and still move forward.
- **Cap per-format lengths** (JPEG/PNG 64 MiB, RAW/CR3 256 MiB) so a bad header can't make the tool read gigabytes. Pick caps from real files: a Fujifilm RAF reached 163 MB, and uncompressed 100 MP files approach 200 MiB.

### D. Fail per item, not per run

- **Wrap each file's save in its own `try`.** Record failures as rows and continue. Re-raise only conditions that doom every later file: disk full (`ENOSPC`, `EDQUOT`). *Without it:* one `rename` error aborted all of Phase 1, and the summary said "Recovered 0" while files sat on disk.
- **Count from the report, not from return values.** A function that raises never returns its count.
- **Create directories before `rename`.** `Path.rename` doesn't create parents. *Without it:* `--verify` crashed on the first broken file when moving it to `suspect/`.
- **Write the report row by row, and flush.** Write the summary in `finally`. Handle `KeyboardInterrupt` in `main()` with a calm message. *Without it:* any crash or Ctrl-C after hours of carving lost every row.
- **Show users one-line errors, not tracebacks.** Raise a `UserError` for problems the user can fix. Map `PermissionError` (hint: sudo), disk full, `EOFError` and Ctrl-C to plain messages. Exit codes: 0 done, 1 error or stopped, 2 bad arguments.

### E. Raw devices are not files (macOS)

All four of these were measured on a real `/dev/rdiskN`:
- **`seek(0, SEEK_END)` returns 0.** *Without the fix:* `recover /dev/rdiskN` saw a size of 0 and silently carved nothing. Take the size from `diskutil info -plist` (`Size` / `TotalSize`).
- **Unaligned reads fail with `EINVAL`, even through Python's buffered reader.** Open devices with `buffering=0` and widen every request to whole `DeviceBlockSize` blocks, then slice.
- **Reading at or past the end returns 0 bytes, not an error.** Loop to the known size, and treat only an empty read as the end. A short read is not the end.
- **Bad sectors make a whole multi-block read fail.** Fall back to block-by-block reads, fill unreadable blocks with zeros, and count them.

Two more points:
- **`/dev/rdiskN` for real cards is `root:operator 0640`, so reading needs `sudo`.** Disk images attached with `hdiutil` are owned by the user who attached them, which makes them ideal test devices.
- **Imaging writes to `<name>.img.part` and renames when complete.** That way an interrupted copy is obvious.

### F. Choose the disk safely (macOS)

- **"Own" disks:**
  - the boot volume's `ParentWholeDisk`, plus the whole disks behind its `APFSPhysicalStores`;
  - any disk that is `Internal` **and not** `RemovableMedia`.

  Don't refuse on `Internal` alone: the built-in SD slot reports `Internal: True, RemovableMedia: True`, and a naive rule would block the very card you want.
- **Hide `BusProtocol == "Disk Image"` by default** (simulator runtimes and similar), but offer a flag for testing.
- **Match volume names case-insensitively.** If two disks share a name, demand the disk id. Names stay visible in `diskutil list` when the card is unmounted, so selection by name still works after unmounting.
- **Read the whole disk (`/dev/rdiskN`), not the partition.** Data outside a re-created partition is still reachable that way.
- **Check `os.access(dev, R_OK)` up front** and say "run with sudo" instead of failing deep inside.

### G. Make results honest

- **Three categories:**
  - *Recovered successfully*: saved and looks complete.
  - *Failed to recover*: saved but damaged, incomplete, failed `--verify`, or could not be saved.
  - *Cannot recover*: the data is gone or unusable, so nothing was saved.
- **No silent drops.** A header over the cap gets a "Cannot recover" row. A file cut at the cap is saved but marked "Failed: incomplete". *Without it:* a 150 MiB RAF vanished, and a 130 MiB TIFF was cut at 120 MiB and labelled a success.
- **Check the signature at the old location.** In Phase 1, check the first bytes at the file's old location against its extension before saving. A mismatch means the data was overwritten.
- **Flag partial overwrites.** Count still-free clusters across the whole file; partly overwritten files are saved but flagged.

### H. Usable under sudo and on real file systems

- **Hand created files back to the real user** with `os.lchown` using `SUDO_UID`/`SUDO_GID`. Do it only for paths the tool created, never a pre-existing folder: `-o /` must not chown the system.
- **Sanitize every path component** from the card:
  - replace illegal and control characters;
  - strip leading and trailing dots and spaces, which also kills `..`;
  - make names unique with `_1`, `_2`, ….

  APFS limits names to 255 *characters* (not bytes), so a collision suffix on a very long name can still fail. Per-file error handling absorbs that.
- **Quote any command you print for copy-paste** with `shlex.quote`. Real paths contain spaces ("Artificial engine").
- **Default output to the current folder.** macOS privacy rules can block `sudo` from writing to Desktop or Documents.

## How we verified it (do this for every change)

1. **Scan, then reproduce before fixing.** List suspected failure paths, then build the smallest input that triggers each one: a truncated GPT, a 1 TiB box, a self-referencing folder. Fix only what reproduces or is certain from the code. Downgrade claims that don't reproduce. Our "endless zero-fill loop" didn't happen on macOS, because reading past the end returns 0 bytes.
2. **Check environment assumptions empirically before writing safety rules.** Print the real `diskutil` fields, measure `SEEK_END`, and test unaligned reads. Two of our assumptions were wrong: the SD slot's `Internal` flag, and the APFS limit being in characters rather than bytes.
3. **Re-run everything after fixing.** That means every reproduction plus a normal-path regression on a real FAT32 image, comparing SHA-256 of the recovered files against the originals.
4. **Use the real card last.** Unmount it, run read-only, image it first, then analyse the copy. Spot-check output by fully decoding a sample: `sips -Z 256 -s format jpeg` handles RAF too. The two files that failed to decode were exactly the two the tool had already flagged.

Recipes for all of this, including the in-process Ctrl-C test (shell background jobs ignore SIGINT), a disk-full test on a tiny `.dmg`, a stub Pillow that forces `--verify` failures, and an AST check for an annotated copy, are in `references/testing.md`. Generate the damaged images with:

```bash
python3 scripts/torture_images.py /tmp/torture    # add --big for the 300 MiB cap test
```

## Quick review checklist

- [ ] Does anything open the source for writing, or put output on its mount points?
- [ ] Does every length or count read from disk pass a range check or the clamped `read_at` before use?
- [ ] Is every loop bounded, and does every recursive walk keep a visited set?
- [ ] Does a single bad file stop only itself, and is disk full the only fatal error?
- [ ] Are report rows flushed as they are written, and is the summary written in `finally`?
- [ ] Device reads: real size from `diskutil`, aligned, unbuffered, bad-block fallback?
- [ ] Is every dropped or cut file reported with a reason, rather than silently skipped?
- [ ] Is every new failure mode reproduced in `torture_images.py` or `testing.md`?

## Reference files

- `references/formats.md`: byte offsets for MBR, GPT, the FAT boot sector and FAT entries, long names, exFAT entry sets and bitmap, plus signatures and length rules for JPEG, PNG, TIFF/CR2/ORF/RW2, RAF and CR3. Read it when touching any parser.
- `references/testing.md`: building a FAT32 test card with `hdiutil`, raw-device checks, and the crash and hang reproductions with expected results. Read it before changing behaviour.
- `scripts/torture_images.py`: writes damaged images, each with the outcome a robust tool should show.
