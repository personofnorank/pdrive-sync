"""One-way, add-only upload of local photos/videos to Proton *Photos*.

Proton Photos is a separate volume from Drive ("My files"), so the Drive sync
never makes anything appear in the Photos timeline. This module uploads media
under PHOTOS_ROOT with `proton-drive photo upload`.

Safety properties (by design):
  * ADD-ONLY: nothing here deletes, trashes, renames or overwrites anything,
    locally or remotely. There is no delete code path in this module.
  * Never touches the originals: files whose date has to be corrected are
    uploaded from a temporary COPY; the original (and its mtime, which the
    Drive sync watches) is left alone.
  * Uploads use `-c skip`, never `rename`/replace.
  * Plan-only unless explicitly applied.

Capture date: the CLI/SDK takes the photo's EXIF capture time and falls back to
the file's mtime when there is no EXIF date. Files synced from Drive often have
an mtime equal to the sync time, so for files where we can find no EXIF date we
derive a better fallback date from the filename (e.g. IMG_20230930_150459.jpg)
and stamp it on the temporary copy only.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import json
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from .logutil import get_logger

log = get_logger()

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".tif",
              ".tiff", ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".m2ts", ".wmv", ".webm"}
MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS
# Proton Photos silently drops RAW files (CLI still exits 0), so they are never sent.
# They stay in Drive and are listed in photos_raw_not_uploaded.txt.
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2"}

# Screenshots are deliberately not uploaded to Photos.
EXCLUDED_DIR_NAMES = {"screenshots"}          # compared case-insensitively
EXCLUDED_NAME_PREFIXES = ("screenshot_",)     # compared case-insensitively

BATCH_MAX_FILES = 200
BATCH_MAX_BYTES = 2 * 1024 ** 3
DATE_TOLERANCE_S = 24 * 3600
_MIN_YEAR, _MAX_YEAR = 1990, datetime.now().year + 1


def photos_root() -> Path:
    return config.SYNC_ROOT / config.PHOTOS_DIR


def stage_dir() -> Path:
    return config.DATA_DIR / "photos-stage"


# --- selection ---------------------------------------------------------------

def is_excluded(rel: str) -> bool:
    """True for screenshots (Screenshots folder at any depth, Screenshot_* files),
    conflict copies and sync scratch files."""
    parts = rel.split("/")
    if any(p.lower() in EXCLUDED_DIR_NAMES for p in parts[:-1]):
        return True
    name = parts[-1]
    if name.lower().startswith(EXCLUDED_NAME_PREFIXES):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in config.DEFAULT_EXCLUDES)


# --- date detection -------------------------------------------------------------

def exif_datetime(path: str) -> Optional[float]:
    """Best-effort EXIF capture time (epoch, naive local) for JPEG files; None if
    absent/unreadable. Only used to decide whether a fallback date is needed --
    the CLI does its own EXIF parsing at upload time."""
    try:
        with open(path, "rb") as f:
            head = f.read(2)
            if head != b"\xff\xd8":
                return None
            while True:
                b = f.read(1)
                if not b:
                    return None
                if b != b"\xff":
                    continue
                m = f.read(1)
                while m == b"\xff":
                    m = f.read(1)
                if not m or m[0] in (0xDA, 0xD9):
                    return None
                if m[0] == 0x01 or 0xD0 <= m[0] <= 0xD7:
                    continue
                ln = struct.unpack(">H", f.read(2))[0]
                seg = f.read(ln - 2)
                if m[0] == 0xE1 and seg.startswith(b"Exif\0\0"):
                    return _tiff_datetime(seg[6:])
    except (OSError, struct.error):
        return None


def _tiff_datetime(t: bytes) -> Optional[float]:
    if t[:2] == b"II":
        e = "<"
    elif t[:2] == b"MM":
        e = ">"
    else:
        return None

    def ifd(off):
        n = struct.unpack(e + "H", t[off:off + 2])[0]
        for i in range(n):
            tag, typ, cnt, val = struct.unpack(e + "HHII", t[off + 2 + i * 12: off + 14 + i * 12])
            yield tag, typ, cnt, val

    def ascii_at(cnt, val):
        s = t[val: val + cnt].split(b"\0")[0].decode("ascii", "ignore")
        return s

    try:
        first = struct.unpack(e + "I", t[4:8])[0]
        found = {}
        exif_off = None
        for tag, typ, cnt, val in ifd(first):
            if tag == 0x8769:
                exif_off = val
            elif tag == 0x0132 and typ == 2:
                found["dt"] = ascii_at(cnt, val)
        if exif_off:
            for tag, typ, cnt, val in ifd(exif_off):
                if tag == 0x9003 and typ == 2:
                    found["orig"] = ascii_at(cnt, val)
                elif tag == 0x9004 and typ == 2:
                    found["dig"] = ascii_at(cnt, val)
        for k in ("orig", "dig", "dt"):
            s = found.get(k)
            if s and not s.startswith("0000"):
                return datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except (struct.error, ValueError, OverflowError, OSError):
        return None
    return None


_NAME_DATE_RES = [
    # strict YYYYMMDD_HHMMSS anywhere (e.g. image_<uuid>20221228_210322.jpg = 2022-12-28 21:03:22);
    # month/day/hour/min/sec ranges are enforced by the pattern itself
    re.compile(r"((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])_([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)"),
    # IMG_20230930_150459, PXL_20230930_150459123, VID-20230930-WA0001, 20230930_150459
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?:[_\-T ]?(\d{2})(\d{2})(\d{2}))?"),
    # 2023-09-30 15.04.59 / 2023-09-30_15-04-59 / 2023-09-30
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.](\d{2})[-_.](\d{2})(?:[ _T\-](\d{2})[-_.:](\d{2})[-_.:](\d{2}))?(?!\d)"),
]


def filename_datetime(name: str) -> Optional[float]:
    """Capture time embedded in a filename (camera/phone naming schemes)."""
    for rx in _NAME_DATE_RES:
        m = rx.search(name)
        if not m:
            continue
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        hh, mi, ss = (int(m[4]), int(m[5]), int(m[6])) if m[4] else (12, 0, 0)
        if not (_MIN_YEAR <= y <= _MAX_YEAR):
            continue
        try:
            return datetime(y, mo, d, hh, mi, ss).timestamp()
        except ValueError:
            continue
    return None


_FOLDER_DAY_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[_\-](\d{2})[_\-](\d{2})(?!\d)")
_FOLDER_MONTH_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[_\-](\d{2})(?!\d)")


def folder_datetime(rel: str) -> Optional[float]:
    """Date encoded in a parent folder name (e.g. Sorted/2008/2008_10/2008_10_01,
    Upload/2020-03). Day-accurate if a day is present, else the 1st of the month."""
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    folder = re.sub(r"(?<!\d)((?:19|20)\d{2})/(0[1-9]|1[0-2])(?:/|$)", r"\1_\2/", folder)
    for rx, has_day in ((_FOLDER_DAY_RE, True), (_FOLDER_MONTH_RE, False)):
        for m in reversed(list(rx.finditer(folder))):
            y, mo = int(m[1]), int(m[2])
            d = int(m[3]) if has_day else 1
            if _MIN_YEAR <= y <= _MAX_YEAR:
                try:
                    return datetime(y, mo, d, 12, 0, 0).timestamp()
                except ValueError:
                    continue
    return None


def _exiftool_dates(paths: list[str]) -> dict[str, float]:
    """Capture times from exiftool (read-only). {} if exiftool is unavailable."""
    out: dict[str, float] = {}
    if not paths or shutil.which("exiftool") is None:
        if paths:
            log.warning("photos: exiftool not found; skipping metadata dates for %d files", len(paths))
        return out
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        try:
            proc = subprocess.run(
                ["exiftool", "-j", "-d", "%Y:%m:%d %H:%M:%S", "-DateTimeOriginal", "-CreateDate",
                 "-MediaCreateDate", "-TrackCreateDate"] + chunk,
                capture_output=True, text=True, timeout=600)
            rows = json.loads(proc.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            log.warning("photos: exiftool failed on a chunk of %d files: %s", len(chunk), e)
            continue
        for row in rows:
            for k in ("DateTimeOriginal", "CreateDate", "MediaCreateDate", "TrackCreateDate"):
                v = str(row.get(k) or "")
                if v and not v.startswith("0000"):
                    try:
                        out[row["SourceFile"]] = datetime.strptime(v[:19], "%Y:%m:%d %H:%M:%S").timestamp()
                        break
                    except ValueError:
                        continue
    return out


def apply_fallback_dates(items: list["PhotoItem"]) -> None:
    """For files without a (JPEG) EXIF date: exiftool metadata, then filename, then
    folder date, then mtime. Only sets a fallback stamped on upload copies."""
    todo = [i for i in items if i.date_source != "exif"]
    ex = _exiftool_dates([i.abs for i in todo])
    for it in todo:
        if it.abs in ex:
            it.date, it.date_source = ex[it.abs], "exiftool"
        elif it.date_source == "mtime":
            fd = folder_datetime(it.rel)
            if fd:
                it.date, it.date_source = fd, "folder"
        it.needs_stage = it.date_source != "mtime" and abs((it.date or 0) - it.mtime) > DATE_TOLERANCE_S


# --- planning -----------------------------------------------------------------

@dataclass
class PhotoItem:
    rel: str
    abs: str
    size: int
    mtime: float
    date: Optional[float] = None      # best-known capture time
    date_source: str = "mtime"        # exif | filename | mtime
    needs_stage: bool = False         # upload from a copy (date stamped and/or renamed)
    upload_name: Optional[str] = None  # unique name for same-named, different-content files
    dup_of: Optional[str] = None       # rel of a byte-identical item that gets uploaded instead


def scan_photos(include_raw: bool = False) -> list[PhotoItem]:
    root = photos_root()
    items: list[PhotoItem] = []
    if not root.is_dir():
        return items
    base = config.SYNC_ROOT
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d.lower() not in EXCLUDED_DIR_NAMES
                             and d not in config.DEFAULT_DIR_EXCLUDES
                             and not any(fnmatch.fnmatch(d, p) for p in config.DEFAULT_EXCLUDES))
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in MEDIA_EXTS or (ext in RAW_EXTS and not include_raw):
                continue
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, base).replace(os.sep, "/")
            if is_excluded(rel) or os.path.islink(ap):
                continue
            try:
                st = os.stat(ap)
            except OSError:
                continue
            items.append(_classify(PhotoItem(rel, ap, st.st_size, st.st_mtime)))
    apply_fallback_dates(items)
    return items


def _classify(it: PhotoItem) -> PhotoItem:
    ex = exif_datetime(it.abs) if it.abs.lower().endswith((".jpg", ".jpeg")) else None
    if ex:
        it.date, it.date_source = ex, "exif"      # CLI reads EXIF itself; no staging
        return it
    fd = filename_datetime(os.path.basename(it.abs))
    if fd:
        it.date, it.date_source = fd, "filename"
        it.needs_stage = abs(fd - it.mtime) > DATE_TOLERANCE_S
        return it
    it.date, it.date_source = it.mtime, "mtime"
    return it


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_name_collisions(items: list[PhotoItem]) -> dict:
    """Photos has a flat namespace and `-c skip` could silently drop a *different*
    photo that shares a name. For every group of items with the same basename
    (case-insensitive) compare bytes:
      * byte-identical files: only the first (by path) is uploaded; the others get
        `dup_of` and are recorded as covered once that upload succeeded;
      * different content: each distinct file is uploaded from a copy under a
        unique, deterministic name (`stem__<8 hex of path hash>.ext`).
    Size is compared first, so only same-size files are ever hashed."""
    groups: dict[str, list[PhotoItem]] = {}
    for it in items:
        groups.setdefault(os.path.basename(it.rel).lower(), []).append(it)
    stats = {"name_groups": 0, "identical_duplicates": 0, "renamed_variants": 0}
    for members in groups.values():
        if len(members) < 2:
            continue
        stats["name_groups"] += 1
        members.sort(key=lambda i: i.rel)
        reps: list[tuple[PhotoItem, Optional[str]]] = []   # (representative, lazy sha)
        for it in members:
            match = None
            for i, (rep, rsha) in enumerate(reps):
                if rep.size != it.size:
                    continue
                if rsha is None:
                    rsha = reps[i] = (rep, _sha256(rep.abs))
                    rsha = reps[i][1]
                if _sha256(it.abs) == rsha:
                    match = rep
                    break
            if match is not None:
                it.dup_of = match.rel
                stats["identical_duplicates"] += 1
            else:
                reps.append((it, None))
        if len(reps) > 1:
            for rep, _ in reps:
                stem, ext = os.path.splitext(os.path.basename(rep.rel))
                tag = hashlib.sha256(rep.rel.encode()).hexdigest()[:8]
                rep.upload_name = f"{stem}__{tag}{ext}"
                rep.needs_stage = True
                stats["renamed_variants"] += 1
    return stats


def raw_report_path() -> Path:
    return config.DATA_DIR / "photos_raw_not_uploaded.txt"


def write_raw_report() -> int:
    """List RAW files that are deliberately not sent to Photos. Returns the count."""
    raws = sorted(i.rel for i in scan_photos(include_raw=True)
                  if os.path.splitext(i.abs)[1].lower() in RAW_EXTS)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_report_path().write_text("".join(f"{r}\n" for r in raws))
    return len(raws)


def held_back_path() -> Path:
    return config.DATA_DIR / "photos_held_back.txt"


class PhotosTimelineError(RuntimeError):
    """The Photos timeline could not be read; uploading without it is refused."""


def skipped_path() -> Path:
    return config.DATA_DIR / "photos_skipped_in_photos.txt"


def unconfirmed_path() -> Path:
    return config.DATA_DIR / "photos_not_in_photos_after_upload.txt"


def write_skipped_report(skipped: list[PhotoItem]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    skipped_path().write_text("".join(f"{i.rel}\n" for i in sorted(skipped, key=lambda i: i.rel)))


def split_held_back(items: list[PhotoItem]) -> tuple[list[PhotoItem], list[PhotoItem]]:
    """Files with no capture date from ANY source (only mtime, which is just the
    sync time) are held back so they don't land at 'today' on the timeline.
    They stay untouched locally and on Drive; they are NOT in Photos."""
    held = [i for i in items if i.date_source == "mtime"]
    return [i for i in items if i.date_source != "mtime"], held


def write_held_report(held: list[PhotoItem]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    held_back_path().write_text("".join(f"{i.rel}\n" for i in sorted(held, key=lambda i: i.rel)))


def summarize(items: list[PhotoItem], pending: list[PhotoItem], coll: dict) -> dict:
    srcs: dict[str, int] = {}
    for it in pending:
        srcs[it.date_source] = srcs.get(it.date_source, 0) + 1
    return {
        "media_files": len(items),
        "already_uploaded": len(items) - len(pending),
        "pending": len(pending),
        "pending_bytes": sum(i.size for i in pending),
        "date_source": srcs,
        "need_date_fix_copy": sum(1 for i in pending if i.needs_stage),
        **coll,
    }


# --- applying -----------------------------------------------------------------

def _batches(items: list[PhotoItem]):
    cur: list[PhotoItem] = []
    size = 0
    for it in items:
        if cur and (len(cur) >= BATCH_MAX_FILES or size + it.size > BATCH_MAX_BYTES):
            yield cur
            cur, size = [], 0
        cur.append(it)
        size += it.size
    if cur:
        yield cur


def _stage(it: PhotoItem, sdir: Path) -> str:
    """Copy (never link) the file, under its unique upload name if it has one, and
    stamp the derived date on the COPY."""
    sdir.mkdir(parents=True, exist_ok=True)
    dst = sdir / (it.upload_name or os.path.basename(it.abs))
    shutil.copy2(it.abs, dst)
    assert it.date is not None
    os.utime(dst, (it.date, it.date))
    return str(dst)


def sync_photos(db, apply: bool = False, limit: Optional[int] = None) -> tuple[dict, int, int]:
    """Plan (apply=False) or perform (apply=True) the upload.

    Returns (summary, uploaded, failed). Only files whose batch the CLI reported
    as successful are recorded in the state DB."""
    from . import proton

    items, held = split_held_back(scan_photos())
    write_held_report(held)
    coll = resolve_name_collisions(items)
    done = db.photo_uploaded_keys()
    pending = [i for i in items if (i.rel, i.size) not in done]
    summary = summarize(items, pending, coll)
    summary["held_back_undated"] = len(held)
    summary["held_back_list"] = str(held_back_path())
    summary["raw_not_uploaded"] = write_raw_report()
    summary["raw_list"] = str(raw_report_path())
    if not apply:
        return summary, 0, 0

    by_rel = {i.rel: i for i in items}
    # Never add what Photos already has (e.g. the phone's own backup): skip by name.
    # If the timeline can't be read or parsed we cannot know, so refuse to upload.
    try:
        remote = remote_names_from_timeline(proton.photo_timeline())
    except Exception as e:
        raise PhotosTimelineError(f"cannot read Photos timeline: {e}") from e
    if not remote:
        raise PhotosTimelineError("Photos timeline was empty or unrecognised")
    in_photos = {i.rel for i in pending if not i.dup_of
                 and (i.upload_name or os.path.basename(i.abs)).lower() in remote}
    in_photos |= {i.rel for i in pending if i.dup_of in in_photos}
    write_skipped_report([i for i in pending if i.rel in in_photos])
    summary["skipped_already_in_photos"] = len(in_photos)
    summary["skipped_list"] = str(skipped_path())
    pending = [i for i in pending if i.rel not in in_photos]
    # duplicates whose representative is already uploaded are covered without upload
    covered = [i for i in pending if i.dup_of and (i.dup_of, by_rel[i.dup_of].size) in done]
    if covered:
        db.photo_mark_uploaded([(i.rel, i.size) for i in covered])
    cov = {i.rel for i in covered}
    todo = [i for i in pending if not i.dup_of and i.rel not in cov]
    if limit is not None:
        todo = todo[:limit]
    dups_of: dict[str, list[PhotoItem]] = {}
    for i in pending:
        if i.dup_of and i.rel not in cov:
            dups_of.setdefault(i.dup_of, []).append(i)
    uploaded = failed = 0
    unconfirmed: list[str] = []
    for batch in _batches(todo):
        sdir = stage_dir() / f"batch-{os.getpid()}-{int(time.time() * 1000)}"
        try:
            paths = [_stage(i, sdir) if i.needs_stage else i.abs for i in batch]
            proton.photo_upload(paths)
        except Exception as e:  # CliError or copy failure: leave the batch unrecorded
            log.warning("photos: batch of %d failed: %s", len(batch), e)
            failed += len(batch)
            continue
        finally:
            _cleanup_stage(sdir)
        # The CLI exits 0 even when it silently drops a file (e.g. RAW), so only record
        # files whose name is actually in the Photos timeline afterwards.
        try:
            seen = remote_names_from_timeline(proton.photo_timeline()) or set()
        except Exception as e:
            log.warning("photos: cannot confirm batch in timeline: %s", e)
            seen = set()
        ok = [i for i in batch if (i.upload_name or os.path.basename(i.abs)).lower() in seen]
        bad = [i for i in batch if i not in ok]
        keys = [(i.rel, i.size) for i in ok]
        keys += [(d.rel, d.size) for i in ok for d in dups_of.get(i.rel, [])]
        if keys:
            db.photo_mark_uploaded(keys)
        uploaded += len(ok)
        failed += len(bad)
        unconfirmed.extend(i.rel for i in bad)
        if bad:
            log.warning("photos: %d file(s) NOT found in Photos after upload (not recorded)", len(bad))
        log.info("photos: confirmed %d of batch of %d (%d total)", len(ok), len(batch), uploaded)
    if unconfirmed:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        unconfirmed_path().write_text("".join(f"{r}\n" for r in sorted(unconfirmed)))
    summary["not_in_photos_after_upload"] = len(unconfirmed)
    if unconfirmed:
        summary["not_in_photos_list"] = str(unconfirmed_path())
    return summary, uploaded, failed


def _cleanup_stage(sdir: Path) -> None:
    """Remove OUR temporary copies only (refuses anything outside the stage dir)."""
    try:
        if sdir.exists() and stage_dir().resolve() in sdir.resolve().parents:
            shutil.rmtree(sdir)
    except OSError as e:
        log.warning("photos: could not remove staging dir %s: %s", sdir, e)


_TIMELINE_LINE_RE = re.compile(
    r"^[A-Z][a-z]{2} \d{1,2} \d{4} \d{1,2}:\d{2} (?:[\d.,]+ [A-Za-z]+|-) (.+)$")


def remote_names_from_timeline(text: str) -> Optional[set[str]]:
    """Best-effort parse of `photo timeline -d` output into lower-cased file names.
    The CLI documents no output format, so accept JSON (any nesting, 'name' keys) or
    one entry per line; return None if nothing recognisable was found."""
    names: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for k in ("name", "fileName", "filename"):
                if isinstance(o.get(k), str):
                    names.add(o[k].lower())
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    try:
        walk(json.loads(text))
    except ValueError:
        # Real `photo timeline -d` output: "Nov 02 2026 13:58 2.54 MiB <name>"
        # (size is "-" for some rows). Names may contain spaces; take the rest of line.
        for line in text.splitlines():
            m = _TIMELINE_LINE_RE.match(line.strip())
            if m:
                names.add(m.group(1).strip().lower())
        if names:
            return names
        for line in text.splitlines():
            for tok in re.findall(r"[^\s\"',|]+\.[A-Za-z0-9]{2,5}\b", line):
                names.add(os.path.basename(tok).lower())
    return names or None


def verify_photos(db, check_remote: bool = True) -> dict:
    """Read-only. Clean only if (1) every local media file is recorded as uploaded
    (or is a byte-identical duplicate of one) and (2) every expected upload name is
    present in the Photos timeline. If the timeline can't be read/parsed the result
    is NOT clean. Deletes nothing anywhere."""
    from . import proton
    items, held = split_held_back(scan_photos())
    write_held_report(held)
    resolve_name_collisions(items)
    done = db.photo_uploaded_keys()
    by_rel = {i.rel: i for i in items}
    not_recorded = [i.rel for i in items if (i.rel, i.size) not in done]
    expected = {}
    for i in items:
        rep = by_rel[i.dup_of] if i.dup_of else i
        expected[i.rel] = (rep.upload_name or os.path.basename(rep.rel)).lower()
    res = {"local_files": len(items), "not_recorded_as_uploaded": len(not_recorded),
           "held_back_undated_NOT_IN_PHOTOS": len(held), "held_back_list": str(held_back_path()),
           "remote_check": "skipped", "missing_in_photos": None, "clean": False,
           "not_recorded_sample": not_recorded[:20]}
    if check_remote:
        try:
            names = remote_names_from_timeline(proton.photo_timeline())
        except Exception as e:  # CliError etc.
            res["remote_check"] = f"failed: {e}"
            return res
        if names is None:
            res["remote_check"] = "failed: could not parse timeline output"
            return res
        missing = sorted(r for r, n in expected.items() if n not in names)
        ghost = sorted(r for r, n in expected.items() if (r, by_rel[r].size) in done and n not in names)
        res["recorded_but_not_in_photos"] = len(ghost)
        res["recorded_but_not_in_photos_sample"] = ghost[:20]
        res.update(remote_check="ok", missing_in_photos=len(missing), missing_sample=missing[:20])
        res["clean"] = not not_recorded and not missing and not ghost
    return res
