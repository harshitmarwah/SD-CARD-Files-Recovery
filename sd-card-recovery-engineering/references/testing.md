# Testing a recovery tool without risking real photos

Never point a test at the user's card. Build throwaway disk images, which on macOS can be attached as real raw devices, and run every reproduction there. Use the real card only for the final read-only run.

## Contents
1. A realistic FAT32 test card
2. Raw-device facts to re-check
3. Reproductions and expected results
4. Hard-to-trigger paths: Ctrl-C, disk full, --verify
5. Regression and final checks

---

## 1. A realistic FAT32 test card (macOS)

```bash
# Test photos: convert any PNG on the system to JPEGs of different quality
sips -s format jpeg -s formatOptions 80 some.png --out p_80.jpg

# 128 MiB card with an MBR partition table, like a camera card
hdiutil create -size 128m -fs "MS-DOS FAT32" -volname SDTEST -layout MBRSPUD sd.dmg
hdiutil attach sd.dmg                       # mounts at /Volumes/SDTEST
mkdir -p /Volumes/SDTEST/DCIM/100CANON && cp p_80.jpg /Volumes/SDTEST/DCIM/100CANON/IMG_0001.JPG
sync && rm /Volumes/SDTEST/DCIM/100CANON/IMG_0001.JPG && sync
hdiutil detach /dev/diskN

# Attach WITHOUT mounting: the realistic "unmounted card" state
hdiutil attach -nomount sd.dmg              # prints /dev/diskN; /dev/rdiskN is owned by you, no sudo
```

Also add a long-name file, a PNG and a nested folder. Delete some files and keep others. After recovery, compare `shasum -a 256` of each recovered file with its original; the hashes should match exactly.

Expected quirks (not bugs):
- FAT 8.3-only names come back with `_` as the first letter.
- macOS creates `._NAME` sidecar files on mounted FAT volumes; these show up as "Cannot recover".

## 2. Raw-device facts to re-check on a new OS version

```python
f = open("/dev/rdiskN", "rb", buffering=0)
f.seek(0, 2)                  # macOS: returns 0, so take the size from `diskutil info -plist`
f.seek(size - 4096); f.read(1 << 20)   # short read at the end: 4096
f.seek(size); f.read(1 << 20)          # at the end: b"" (no error)
f.seek(1000); f.read(92)               # unaligned: OSError EINVAL (buffered open too)
```

Also print the `diskutil info -plist` fields used for safety (`Internal`, `RemovableMedia`, `BusProtocol`, `ParentWholeDisk`, `APFSPhysicalStores`) for every disk before trusting a rule. A built-in SD slot is `Internal: True, RemovableMedia: True`.

## 3. Reproductions and expected results

`scripts/torture_images.py OUTDIR` writes the synthetic ones. Run the tool's `recover` on each one; none may print a traceback.

| Input | Failure before the fix | Robust result |
|---|---|---|
| GPT header cut to 78 bytes | `struct.error` traceback | "carving only", exit 0 |
| GPT header with 2^32-1 entries of 2^32-1 bytes | `OverflowError` | "carving only", exit 0 |
| CR3 whose box claims 1 TiB, then a real JPEG | 1 TiB "exact" file; the JPEG swallowed | CR3 → Cannot recover (past end); JPEG → Recovered |
| RAF header claiming 300 MiB | Silently skipped | Cannot recover: larger than the size limit |
| RAF claiming 150 MiB on a 100 MiB card | Silently skipped | Cannot recover: runs past the end of the card |
| TIFF-style RAW running 300 MiB (`--big`) | Cut at the cap, labelled success | Failed to recover: cut at the size limit |
| JPEG cut off before `FF D9` | — | Nothing carved, no crash |
| Empty image file | Carving silently scanned nothing | Clear error "is empty" |
| Four FAT32 folder entries pointing at the root cluster | Hang (4^16 visits) | Finishes in well under a second |

Self-referencing folder recipe (needs the FAT32 test image from section 1):

```python
import struct, shutil
shutil.copy("card.img", "loop.img")
img = Image("loop.img"); v = find_volumes(img)[0]            # the tool's own parser
off = v.cluster_off(v.root_cluster); root = img.read_at(off, v.csize)
free = next(i for i in range(0, len(root), 32) if root[i] == 0)
ents = b""
for k in range(4):
    e = bytearray(32); e[:11] = f"LOOP{k}      ".encode()[:11]; e[11] = 0x10
    struct.pack_into("<H", e, 26, v.root_cluster & 0xFFFF)
    struct.pack_into("<H", e, 20, v.root_cluster >> 16)
    ents += bytes(e)
with open("loop.img", "r+b") as f: f.seek(off + free); f.write(ents)
```

## 4. Hard-to-trigger paths

**`--verify` failures without broken photos.** Put a stub Pillow first on the path:

```bash
mkdir -p stubpil/PIL && touch stubpil/PIL/__init__.py
printf 'def open(p):\n    raise OSError("stub: broken")\n' > stubpil/PIL/Image.py
PYTHONPATH=stubpil python3 sd_recover.py recover card.img -o out --verify
```

Expected: every file goes to `suspect/`, all are counted as "Failed to recover", and there's no crash. Check both phases. For Phase 2, use an image with no filesystem, just a JPEG at an offset.

**Ctrl-C.** Shell background jobs (`cmd &` in a non-interactive shell) **ignore SIGINT**, so `kill -INT` silently does nothing and the test passes falsely. Interrupt from inside Python instead:

```python
import sys, threading, _thread
sys.argv = ["sd_recover.py", "recover", "big.img", "-o", "out"]   # big.img: card.img truncated up to 30 GiB (sparse)
threading.Timer(1.0, _thread.interrupt_main).start()
import sd_recover; sd_recover.main()     # expect SystemExit("Stopped. ..."), report.csv + summary.csv present
```

**Disk full.** Make the output volume tiny:

```bash
hdiutil create -size 600k -fs HFS+ -volname TINYOUT tiny.dmg && hdiutil attach tiny.dmg
python3 sd_recover.py recover card.img -o /Volumes/TINYOUT/rec   # expect one-line "no space left" error, exit 1, CSVs kept
```

**Safety refusals** (all must print an error and create nothing):
- `scan` with the name of a Mac disk ("Macintosh HD");
- `image /dev/rdisk3 x.img` and `image /dev/rdisk0s2 x.img`, i.e. the system disks;
- `image` to a destination that already exists;
- an output folder or image path on the mounted test card;
- answering `n` at the confirmation prompt.

Never test the swapped-arguments case against a real device.

## 5. Regression and final checks

- After every fix, re-run **all** reproductions plus the normal path, and compare SHA-256 hashes.
- Run under the oldest supported Python too (`/usr/bin/python3` is 3.9 on macOS).
- For an annotated copy of the program, insert comment lines with a script and assert `ast.dump(ast.parse(original)) == ast.dump(ast.parse(annotated))`, so the explanations can never change the code. Docstrings count as code, so add comments with `#` only.
- On the real card:
  1. unmount it (`diskutil unmountDisk /dev/diskN`);
  2. run with `sudo`, letting it copy the card first;
  3. spot-check output by fully decoding a sample: `sips -Z 256 -s format jpeg FILE --out /tmp/t.jpg` handles RAF too;
  4. the files that fail to decode should be exactly the ones the report already flagged.
