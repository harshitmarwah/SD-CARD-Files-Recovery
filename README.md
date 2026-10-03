# SD-CARD-Files-Recovery

Recover deleted photos from an SD card with `sd_recover.py`. JPEG, PNG and common camera RAW formats are supported: Fujifilm RAF, Canon CR2/CR3, Nikon NEF, Sony ARW, DNG, Olympus ORF, Panasonic RW2 and others.

It is a single Python file that uses only the standard library, so there is nothing to install. The card is only ever **read**, never written to. On a Mac it lists the cards it can see and asks which one to scan. It never reads the Mac's own disks.

```bash
sudo python3 sd_recover.py
```

---

## Contents

- [Before you start: protect your photos](#before-you-start-protect-your-photos)
- [Requirements](#requirements)
- [Quick start (macOS)](#quick-start-macos)
- [Manual mode (Linux, Windows, macOS)](#manual-mode-linux-windows-macos)
- [Options](#options)
- [What you get](#what-you-get)
- [How it works](#how-it-works)
- [Limitations](#limitations)
- [FAQ](#faq)
- [Troubleshooting](#troubleshooting)
- [License and disclaimer](#license-and-disclaimer)

---

## Before you start: protect your photos

A deleted photo stays on the card until something writes over it. Every write makes recovery less likely.

1. **Stop using the card right away.** Don't take new pictures with it, copy files to it or format it.
2. **Don't open the card in Finder or Explorer.** Your computer writes small hidden files to any card it mounts. On a Mac these are Spotlight indexes, `.fseventsd` and `._*` files.
3. **Unmount the card before scanning, but leave it in the reader.** The program can still read an unmounted card:
   ```bash
   diskutil list                      # find the card, e.g. /dev/disk4 (check the size and name)
   diskutil unmountDisk /dev/disk4    # unmount without ejecting
   ```
4. **Save results to your computer's disk, never to the card.** The program refuses to write onto the card it is reading.

---

## Requirements

| | |
|---|---|
| Python | 3.9 or newer. macOS ships 3.9 at `/usr/bin/python3`. Tested with 3.9 and 3.14. |
| Dependencies | None. Optionally install [Pillow](https://pypi.org/project/pillow/) (`pip install Pillow`) to use `--verify`. |
| Permissions | Administrator rights (`sudo` on macOS/Linux, an admin terminal on Windows) to read the card. |
| Free disk space | About the card's size plus 1 GB, for the copy it makes first. With less space, it reads the card directly. |
| Card filesystems | FAT16, FAT32 and exFAT, with an MBR, GPT or no partition table. Signature scanning works on any card, even formatted or unreadable ones. |

**Platform status:** everything is tested on macOS with a built-in SD reader. Picking a card by name (`scan`) is macOS-only. `image` and `recover` are written to work on Linux and Windows but haven't been tested there yet. Reports are welcome.

---

## Quick start (macOS)

1. Download `sd_recover.py`, or clone this repository, and open Terminal in that folder.
   ```bash
   git clone https://github.com/harshitmarwah/SD-CARD-Files-Recovery.git
   cd SD-CARD-Files-Recovery
   ```
2. Put the card in the reader and unmount it, but don't eject it (see above).
3. Run it:
   ```bash
   sudo python3 sd_recover.py
   ```
4. Type your Mac password. It doesn't show as you type, which is normal.
5. Type the card's name from the list, then `y` to confirm.

Example session (abridged):

```text
Cards and external disks (your Mac's own disks are never listed or read):
  Untitled                     disk4      124.7 GB  Secure Digital

Name of the card to scan (or its disk id, e.g. disk4): Untitled

The whole card (disk4, 124.7 GB) will be read. Results go to /Users/you/SD-CARD-Files-Recovery/recovered/
Continue? [y/N] y
Copying the card to /Users/you/SD-CARD-Files-Recovery/recovered/Untitled.img ...
  copying: 100%
Copied 124,675,686,400 bytes
Found exFAT volume at byte 1,048,576 (cluster 131,072 B)
Phase 1: scanning exFAT for deleted files...
Phase 2: carving free space for photo signatures...
  carving: 100%  (10 found)

Recovered successfully: 813   Failed to recover: 1   Cannot recover: 5
Files:   /Users/you/SD-CARD-Files-Recovery/recovered/
Details: /Users/you/SD-CARD-Files-Recovery/recovered/report.csv and /Users/you/SD-CARD-Files-Recovery/recovered/summary.csv
```

On a Mac with a built-in SD reader, copying a 128 GB card took about 10 minutes, and the recovery passes a few minutes more.

Without the questions (for scripts):

```bash
sudo python3 sd_recover.py scan --volume Untitled -o ~/recovered
```

---

## Manual mode (Linux, Windows, macOS)

Make a copy of the card first, then recover from the copy. The card is read only once, and you can re-run recovery as often as you like.

```bash
# 1. Copy the card. The destination must be a new file on your computer.
sudo python3 sd_recover.py image /dev/sdX card.img            # Linux (whole disk, not /dev/sdX1)
sudo python3 sd_recover.py image /dev/rdiskN card.img         # macOS (rdisk is faster)
python sd_recover.py image \\.\PhysicalDriveN card.img        # Windows, in an administrator terminal

# 2. Recover from the copy (no sudo needed).
python3 sd_recover.py recover card.img -o recovered
```

`recover` also accepts a device path directly. On macOS it then uses the device's real size and block-aligned reads.

---

## Options

Running it with no command is the same as `scan`.

### `scan` (macOS): pick the card by name, copy it, recover

| Option | Meaning |
|---|---|
| `--volume NAME` | Card name as shown in Finder, or a disk id such as `disk4`. Skips both questions. Not case-sensitive. |
| `--direct` | Read the card directly instead of copying it first. Uses no extra disk space, but the card is read for the whole run. |
| `--allow-disk-images` | Also list mounted disk images (`.dmg`), which are hidden by default. Useful for testing. |

### `image SOURCE DEST`: sector-by-sector copy

Copies the whole device to a **new** file. It refuses to overwrite an existing file, and on macOS it also refuses to write onto the card itself or to read the Mac's own disks. Unreadable sectors are filled with zeros, and the count is reported.

### `recover IMAGE`: recover from a copy (or device)

### Options for `scan` and `recover`

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output DIR` | `./recovered` | Where recovered files and reports go. |
| `--all-types` | off | Phase 1 restores every deleted file (videos, documents, ...), not just photos. |
| `--no-carve` | off | Skip Phase 2 (signature scanning). |
| `--min-size BYTES` | `10240` | Ignore carved files smaller than this. This skips embedded thumbnails. |
| `--verify` | off | Decode every recovered JPEG/PNG with Pillow and move broken ones to `suspect/`. |

---

## What you get

```text
recovered/
├── filesystem/     Phase 1: original names and folders, e.g. DCIM/101_FUJI/DSCF5414.RAF
├── carved/         Phase 2: files found by signature, named by position, e.g. f_0000c4a20000.raf
├── suspect/        only with --verify: files that saved but don't decode
├── report.csv      one row per file, with full details
├── summary.csv     three columns: Recovered successfully | Failed to recover | Cannot recover
└── <name>.img      scan only: the full copy of the card. Delete it when you're done.
```

### Result categories

| Result | Meaning | Typical `detail` |
|---|---|---|
| **Recovered successfully** | The file was saved and looks complete. | `intact`, `carved`, `carved - length estimated, format has no end marker` |
| **Failed to recover** | The file was saved but is damaged or incomplete, or could not be saved. | `saved, but partly overwritten - probably corrupt`, `saved to suspect/: picture does not decode`, `saved, but incomplete: cut at the 256 MiB size limit`, `could not save: ...` |
| **Cannot recover** | The data is gone or unusable, so nothing was saved. | `overwritten by newer data`, `overwritten - data at its location is something else`, `directory entry damaged - bad cluster range`, `larger than the 256 MiB size limit, or header damaged` |

### `report.csv` columns

| Column | Meaning |
|---|---|
| `result` | One of the three categories above. |
| `method` | `filesystem` (Phase 1) or `carving` (Phase 2). |
| `original_name` | Path on the card (Phase 1), or `<type> at byte N` for carving problems. |
| `saved_as` | Path inside the output folder; empty if nothing was saved. |
| `byte_offset` | Where the data starts on the card. |
| `size` | Bytes. |
| `detail` | Why the file got its result. |
| `sha256` | Fingerprint of the saved file, used to skip duplicates between the two phases. |

`report.csv` is written row by row, so it survives Ctrl-C or a crash. `summary.csv` is written when the run ends, including after Ctrl-C.

---

## How it works

For a line-by-line walkthrough of the code, read [`sd_recover_annotated.py`](sd_recover_annotated.py). It is the same program with explanatory comments added; run `sd_recover.py`.

1. **Copy (scan only).** The whole card is copied byte by byte into `<name>.img`, so the card is read once. If there isn't enough free space, the card is read directly instead.
2. **Find filesystems.** It reads the partition table (MBR or GPT) and locates FAT16, FAT32 or exFAT volumes.
3. **Phase 1: filesystem recovery.** Deleting a file only marks its directory entry as deleted and its clusters as free; the data stays where it was. The program walks every folder, deleted folders included, and finds deleted entries. It checks that the first cluster is still free and still starts with the right file signature, then copies the file out with its original name. It assumes each file was stored in one contiguous run, which is how cameras normally write.
4. **Phase 2: carving.** It scans every free cluster for photo signatures (JPEG `FF D8 FF`, PNG, TIFF `II*`/`MM*`, `FUJIFILM`, `ftypcrx`, ...). It then works out each file's length from its internal structure: JPEG segments, PNG chunks, CR3 boxes or the RAF header. This finds photos whose directory entries are gone, for example after a quick format. Files already recovered in Phase 1 are skipped by SHA-256.

---

## Limitations

- **Overwritten data is gone.** No software can bring it back.
- **Fragmented files.** Phase 1 assumes a file was stored in one piece. A file that was split up comes back corrupt; check `report.csv` and use `--verify`.
- **FAT short names.** On FAT16/32, a deleted file without a long name loses its first character, so `IMG_0001.JPG` comes back as `_MG_0001.JPG`. exFAT keeps full names.
- **Carved files have no original names.** They are named by their byte position. RAW files that share the TIFF format (NEF, ARW, DNG, PEF, ...) are saved as `.tif` (CR2 as `.cr2`), so rename them if your viewer needs the real extension.
- **Estimated lengths.** TIFF-style RAW, ORF and RW2 have no end marker, so the carved length is an estimate. Most viewers still open them.
- **Size caps (carving only).** JPEG/PNG 64 MiB, RAW/CR3 256 MiB. Larger finds are reported, not silently dropped.
- **Not covered.** FAT12 (cards under about 32 MB), NTFS/APFS/HFS+ filesystem recovery (carving still works on them), HEIC/HEIF and video carving (Phase 1 with `--all-types` can still restore videos), and cards that were securely erased or low-level formatted.

---

## FAQ

**Why does it need my password even though the card is unmounted?**
Unmounting only removes the files-and-folders view; the card is still attached as a raw device such as `/dev/rdisk4`. Deleted photos are only reachable by reading that raw device byte by byte, and macOS and Linux limit raw device access to administrators. The password lets the program read the card. It opens the device read-only.

**Why is `Untitled.img` as big as the card when my photos are much smaller?**
The image is a copy of every sector of the card, including empty and overwritten space, because deleted photos can be anywhere. The recovered files are only the photo data found inside it. Delete the image once you're happy with the results.

**What are the `._DSCF1234.JPG` rows under "Cannot recover"?**
They are hidden sidecar files macOS creates on cards it has mounted, not photos. You can ignore them.

**Can I scan again without touching the card?**
Yes. At the end, `scan` prints a command that recovers from the saved copy:
```bash
python3 sd_recover.py recover recovered/Untitled.img -o recovered_2
```

**Who owns the output files when I use `sudo`?**
You do. The program hands everything it created back to your account at the end, so you can open and delete the files without `sudo`.

---

## Troubleshooting

| Message | What to do |
|---|---|
| `no permission to read /dev/rdiskN - run it again with sudo` | Run it with `sudo`. |
| `Operation not permitted` even with `sudo` | Give your terminal app Full Disk Access (System Settings › Privacy & Security), or use an output folder outside Desktop/Documents. |
| `'NAME' is not one of the disks listed above` | Type the name exactly as listed, or use the disk id (e.g. `disk4`). The Mac's own disks are never offered. |
| `no SD card or external disk found` | Insert the card and check it appears in `diskutil list`. |
| `... is on the card itself` | Choose an output folder on your computer, not on the card. |
| `... already exists - choose a new file name` | `image` never overwrites; pick a new destination name. |
| `no space left where the files are being saved` | Free space and run again. Files saved so far and both CSVs are kept. Or use `--direct`. |
| `the card stopped responding after N bytes` | The card or reader disconnected. Reinsert it and run again; the partial copy is kept as `.img.part`. |
| Only `carving` results, no `filesystem` results | The card was formatted or its folders are damaged. Carved files are named by position. |

Exit codes: `0` finished, `1` error or stopped with Ctrl-C, `2` invalid arguments.

---

## License and disclaimer

[MIT](LICENSE). The program is designed never to write to the card, but it is provided as is, without warranty. If the photos matter, keep the card untouched until you've checked the results. Only use it on cards you own or are authorised to access.
