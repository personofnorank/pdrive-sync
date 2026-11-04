"""Photos upload tests: exclusions, date handling, add-only behaviour (stubbed CLI)."""
import importlib
import os
import struct
import sys
import time
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_jpeg(path, when=None):
    """Minimal JPEG; with `when` it carries an EXIF DateTimeOriginal."""
    body = b"\xff\xd8"
    if when:
        s = when.strftime("%Y:%m:%d %H:%M:%S").encode() + b"\0"
        # TIFF (LE): IFD0 with ExifIFD pointer -> ExifIFD with DateTimeOriginal
        tiff = b"II*\0" + struct.pack("<I", 8)
        tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, 26) + struct.pack("<I", 0)
        tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, len(s), 44) + struct.pack("<I", 0)
        tiff += s
        seg = b"Exif\0\0" + tiff
        body += b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
    body += b"\xff\xd9"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "sync"
    data = tmp_path / "data"
    root.mkdir(); data.mkdir()
    monkeypatch.setenv("PDRIVE_SYNC_ROOT", str(root))
    monkeypatch.setenv("PDRIVE_DATA_DIR", str(data))
    import pdrive_sync.config as config
    importlib.reload(config)
    config.ensure_dirs()
    from pdrive_sync import state, proton, photos
    importlib.reload(state); importlib.reload(proton); importlib.reload(photos)
    calls = []
    monkeypatch.setattr(photos, "_exiftool_dates", lambda paths: {})
    remote = ["Jan 01 2000 00:00 - unrelated_zz.jpg"]  # simulated Photos library

    def fake_upload(paths):
        calls.append(list(paths))
        remote.extend(f"Nov 03 2026 15:56 1 KiB {os.path.basename(p)}" for p in paths)
        return ""
    monkeypatch.setattr(proton, "photo_upload", fake_upload)
    monkeypatch.setattr(proton, "photo_timeline", lambda: "\n".join(remote) + "\n")
    db = state.StateDB()
    yield type("E", (), dict(root=root, db=db, photos=photos, proton=proton, calls=calls, remote=remote))
    db.close()


def test_screenshots_excluded(env):
    p = env.root / "Pictures"
    make_jpeg(p / "trip" / "a.jpg")
    make_jpeg(p / "Screenshots" / "b.jpg")
    make_jpeg(p / "x" / "screenshots" / "c.jpg")
    make_jpeg(p / "x" / "Screenshot_20230101-101010.jpg")
    make_jpeg(p / "x" / "screenshot_foo.png")
    (p / "x" / "notes.txt").write_text("not media")
    rels = [i.rel for i in env.photos.scan_photos()]
    assert rels == ["Pictures/trip/a.jpg"]


def test_exif_date_read(env):
    f = env.root / "Pictures" / "e.jpg"
    when = datetime(2019, 7, 4, 12, 30, 15)
    make_jpeg(f, when)
    assert env.photos.exif_datetime(str(f)) == when.timestamp()
    make_jpeg(env.root / "Pictures" / "n.jpg")
    assert env.photos.exif_datetime(str(env.root / "Pictures" / "n.jpg")) is None


@pytest.mark.parametrize("name,expected", [
    ("IMG_20230930_150459.jpg", datetime(2023, 9, 30, 15, 4, 59)),
    ("PXL_20230930_150459123.jpg", datetime(2023, 9, 30, 15, 4, 59)),
    ("VID-20210102-WA0001.mp4", datetime(2021, 1, 2, 12, 0, 0)),
    ("2018-05-06 07.08.09.jpg", datetime(2018, 5, 6, 7, 8, 9)),
    ("DSC00012.jpg", None),
    ("IMG_99999999.jpg", None),
])
def test_filename_dates(env, name, expected):
    got = env.photos.filename_datetime(name)
    assert got == (expected.timestamp() if expected else None)


def test_exiftool_date_beats_filename_and_folder(env):
    f = env.root / "Pictures" / "Sorted" / "2008_10_01" / "IMG_20150601_101010.mp4"
    make_jpeg(f)
    want = datetime(2008, 10, 1, 9, 8, 7).timestamp()
    env.photos._exiftool_dates = lambda paths: {p: want for p in paths}
    (it,) = env.photos.scan_photos()
    assert it.date_source == "exiftool" and it.date == want and it.needs_stage


def test_folder_date_fallback_and_mtime_last_resort(env):
    a = env.root / "Pictures" / "Sorted" / "2008" / "2008_10" / "2008_10_16" / "mvi_1262.avi"
    b = env.root / "Pictures" / "misc" / "plain.gif"
    make_jpeg(a); make_jpeg(b)
    items = {i.rel.rsplit("/", 1)[1]: i for i in env.photos.scan_photos()}
    assert items["mvi_1262.avi"].date_source == "folder"
    assert items["mvi_1262.avi"].date == datetime(2008, 10, 16, 12).timestamp()
    assert items["plain.gif"].date_source == "mtime" and not items["plain.gif"].needs_stage
    assert env.photos.folder_datetime("Upload/2020-03/x.heic") == datetime(2020, 3, 1, 12).timestamp()


def test_plan_does_not_upload_and_original_mtime_untouched(env):
    f = env.root / "Pictures" / "IMG_20150601_101010.jpg"
    make_jpeg(f)
    before = os.stat(f).st_mtime
    summary, up, failed = env.photos.sync_photos(env.db, apply=False)
    assert (up, failed) == (0, 0) and env.calls == []
    assert summary["pending"] == 1 and summary["need_date_fix_copy"] == 1
    assert os.stat(f).st_mtime == before


def test_apply_stages_copy_with_capture_date_and_leaves_original(env):
    f = env.root / "Pictures" / "IMG_20150601_101010.jpg"
    make_jpeg(f)
    orig_mtime = os.stat(f).st_mtime
    seen = {}

    def fake(paths):
        assert len(paths) == 1 and paths[0] != str(f)
        seen["mtime"] = os.stat(paths[0]).st_mtime
        env.calls.append(paths)
        env.remote.append(f"Nov 03 2026 15:56 1 KiB {os.path.basename(paths[0])}")
    env.proton.photo_upload = fake
    _, up, failed = env.photos.sync_photos(env.db, apply=True)
    assert (up, failed) == (1, 0)
    assert seen["mtime"] == datetime(2015, 6, 1, 10, 10, 10).timestamp()
    assert f.exists() and os.stat(f).st_mtime == orig_mtime
    assert not list(env.photos.stage_dir().glob("*"))          # temp copies removed


def test_exif_files_uploaded_in_place(env):
    f = env.root / "Pictures" / "e.jpg"
    make_jpeg(f, datetime(2019, 7, 4, 12, 0, 0))
    env.photos.sync_photos(env.db, apply=True)
    assert env.calls == [[str(f)]]


def test_second_run_skips_already_uploaded(env):
    make_jpeg(env.root / "Pictures" / "e.jpg", datetime(2019, 7, 4, 12, 0, 0))
    env.photos.sync_photos(env.db, apply=True)
    env.photos.sync_photos(env.db, apply=True)
    assert len(env.calls) == 1


def test_failed_batch_not_recorded_and_files_kept(env):
    f = env.root / "Pictures" / "e.jpg"
    make_jpeg(f, datetime(2019, 7, 4, 12, 0, 0))

    def boom(paths):
        raise env.proton.CliError("nope")
    env.proton.photo_upload = boom
    _, up, failed = env.photos.sync_photos(env.db, apply=True)
    assert (up, failed) == (0, 1) and f.exists()
    assert env.db.photo_uploaded_keys() == set()


def _jpg_bytes(path, payload):
    make_jpeg(path, datetime(2019, 1, 1, 1, 1, 1))
    with open(path, "ab") as f:
        f.write(payload)


def test_identical_same_name_uploaded_once_all_recorded(env):
    for d in "abc":
        _jpg_bytes(env.root / "Pictures" / d / "IMG_0001.jpg", b"same")
    summary, up, failed = env.photos.sync_photos(env.db, apply=True)
    assert summary["identical_duplicates"] == 2 and summary["renamed_variants"] == 0
    assert (up, failed) == (1, 0) and len(env.calls) == 1
    assert os.path.basename(env.calls[0][0]) == "IMG_0001.jpg"
    assert len(env.db.photo_uploaded_keys()) == 3          # dups covered by the one upload
    env.photos.sync_photos(env.db, apply=True)
    assert len(env.calls) == 1


def test_different_content_same_name_each_gets_unique_name(env):
    _jpg_bytes(env.root / "Pictures" / "a" / "IMG_0001.jpg", b"one1")      # same size,
    _jpg_bytes(env.root / "Pictures" / "b" / "IMG_0001.jpg", b"two2")      # different bytes
    _jpg_bytes(env.root / "Pictures" / "c" / "IMG_0001.jpg", b"three3")    # different size
    names = []

    def fake(paths):
        names.extend(os.path.basename(p) for p in paths)
        env.remote.extend(f"Nov 03 2026 15:56 1 KiB {os.path.basename(p)}" for p in paths)
        for p in paths:
            assert os.path.exists(p) and "/photos-stage/" in p
    env.proton.photo_upload = fake
    summary, up, failed = env.photos.sync_photos(env.db, apply=True)
    assert summary["renamed_variants"] == 3 and (up, failed) == (3, 0)
    assert len(set(n.lower() for n in names)) == 3
    assert all(n.startswith("IMG_0001__") and n.endswith(".jpg") for n in names)
    for d in "abc":                                                        # originals untouched
        assert (env.root / "Pictures" / d / "IMG_0001.jpg").exists()


def test_case_insensitive_name_collision(env):
    _jpg_bytes(env.root / "Pictures" / "a" / "IMG_1.JPG", b"x1")
    _jpg_bytes(env.root / "Pictures" / "b" / "img_1.jpg", b"y2")
    summary, up, _ = env.photos.sync_photos(env.db, apply=False)
    assert summary["name_groups"] == 1 and summary["renamed_variants"] == 2


def test_photo_upload_always_skip_and_no_delete_paths(env, monkeypatch):
    import subprocess
    seen = {}

    def fake_run(args, timeout=300):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")
    importlib.reload(env.proton)
    monkeypatch.setattr(env.proton, "_run", fake_run)
    env.proton.photo_upload(["/x/a.jpg"])
    assert seen["args"][:4] == ["photo", "upload", "-c", "skip"]
    src = open(env.photos.__file__).read()
    # the only remote call is photo_upload; no remote delete/trash and no local unlink
    assert "proton.photo_upload" in src
    for word in ("proton.delete", "proton.trash", "proton.remove", "unlink(", "os.remove("):
        assert word not in src


def test_verify_clean_and_not_clean(env):
    _jpg_bytes(env.root / "Pictures" / "a" / "IMG_1.jpg", b"x1")
    _jpg_bytes(env.root / "Pictures" / "b" / "IMG_1.jpg", b"y2")
    _jpg_bytes(env.root / "Pictures" / "c" / "IMG_1.jpg", b"y2")   # dup of b
    _jpg_bytes(env.root / "Pictures" / "z.jpg", b"zz")
    names = []
    def up_(paths):
        names.extend(os.path.basename(p) for p in paths)
        env.remote.extend(f"Nov 03 2026 15:56 1 KiB {os.path.basename(p)}" for p in paths)
    env.proton.photo_upload = up_
    r = env.photos.verify_photos(env.db, check_remote=False)
    assert not r["clean"] and r["not_recorded_as_uploaded"] == 4
    env.photos.sync_photos(env.db, apply=True)
    env.proton.photo_timeline = lambda: "\n".join(f"- {n}" for n in names)
    r = env.photos.verify_photos(env.db)
    assert r["clean"] and r["remote_check"] == "ok" and r["missing_in_photos"] == 0
    env.proton.photo_timeline = lambda: "\n".join(f"- {n}" for n in names[1:])
    r = env.photos.verify_photos(env.db)
    assert not r["clean"] and r["missing_in_photos"] >= 1
    env.proton.photo_timeline = lambda: "garbage with no names"
    assert env.photos.verify_photos(env.db)["remote_check"].startswith("failed")


def test_verify_json_timeline_and_cli_failure(env):
    assert env.photos.remote_names_from_timeline('[{"name":"A.JPG"},{"x":{"fileName":"b.png"}}]') == {"a.jpg", "b.png"}
    def boom():
        raise env.proton.CliError("auth")
    env.proton.photo_timeline = boom
    _jpg_bytes(env.root / "Pictures" / "a.jpg", b"1")
    r = env.photos.verify_photos(env.db)
    assert not r["clean"] and "auth" in r["remote_check"]


def test_timeline_text_lines(env):
    txt = ("Nov 02 2026 13:58 2.54 MiB IMG_20261102_134512121.jpg\n"
           "Aug 11 2025 17:59 - IMG_20250811_133225.jpg\n"
           "Oct 30 2026 21:41 1.00 KiB My Photo 1.jpg\n")
    assert env.photos.remote_names_from_timeline(txt) == {
        "img_20261102_134512121.jpg", "img_20250811_133225.jpg", "my photo 1.jpg"}


def test_embedded_stamp_and_year_month_folders(env):
    f = env.photos.filename_datetime
    assert f("image_0284b04f-a895-4e53-9b95-1c3366aabfd420221228_210322.jpg") == datetime(2022, 12, 28, 21, 3, 22).timestamp()
    assert f("IMG_20230930_150459.jpg") == datetime(2023, 9, 30, 15, 4, 59).timestamp()
    assert env.photos.folder_datetime("Camera Roll 1/2024/09/IMG_3917.jpeg.heic") == datetime(2024, 9, 1, 12).timestamp()
    assert env.photos.folder_datetime("Camera Roll/2024/10/APSA AGM.jpg") == datetime(2024, 10, 1, 12).timestamp()


def test_undated_files_are_held_back_not_uploaded(env):
    (env.root / "Pictures" / "Unsorted").mkdir(parents=True, exist_ok=True)
    (env.root / "Pictures" / "Unsorted" / "plain.jpg").write_bytes(b"\xff\xd8\xff\xd9 no exif")
    _jpg_bytes(env.root / "Pictures" / "IMG_20230930_150459.jpg", b"dated")
    sent = []
    def up_(paths):
        sent.extend(os.path.basename(p) for p in paths)
        env.remote.extend(f"Nov 03 2026 15:56 1 KiB {os.path.basename(p)}" for p in paths)
    env.proton.photo_upload = up_
    summary, up, failed = env.photos.sync_photos(env.db, apply=True)
    assert sent == ["IMG_20230930_150459.jpg"] and up == 1 and failed == 0
    assert summary["held_back_undated"] == 1
    assert env.photos.held_back_path().read_text().strip() == "Pictures/Unsorted/plain.jpg"
    assert (env.root / "Pictures" / "Unsorted" / "plain.jpg").exists()
    env.proton.photo_timeline = lambda: "- " + sent[0]
    r = env.photos.verify_photos(env.db)
    assert r["held_back_undated_NOT_IN_PHOTOS"] == 1 and r["clean"]


def test_apply_skips_names_already_in_photos_and_refuses_without_timeline(env):
    _jpg_bytes(env.root / "Pictures" / "a" / "have.jpg", b"1")
    _jpg_bytes(env.root / "Pictures" / "b" / "new.jpg", b"2")
    _jpg_bytes(env.root / "Pictures" / "c" / "have.jpg", b"1")  # identical dup of the skipped one
    env.remote.append("Nov 02 2026 13:58 2.54 MiB HAVE.jpg")
    summary, up, failed = env.photos.sync_photos(env.db, apply=True)
    sent = [os.path.basename(p) for c in env.calls for p in c]
    assert sent == ["new.jpg"] and up == 1 and failed == 0
    assert summary["skipped_already_in_photos"] == 2
    assert env.photos.skipped_path().read_text().count("have.jpg") == 2
    assert env.db.photo_uploaded_keys() and all("have.jpg" not in k[0] for k in env.db.photo_uploaded_keys())
    # unreadable / empty / failing timeline: refuse, upload nothing, record nothing
    for bad in (lambda: "garbage", lambda: "", lambda: (_ for _ in ()).throw(env.proton.CliError("auth"))):
        env.calls.clear()
        env.proton.photo_timeline = bad
        _jpg_bytes(env.root / "Pictures" / "d" / "other.jpg", b"3")
        with pytest.raises(env.photos.PhotosTimelineError):
            env.photos.sync_photos(env.db, apply=True)
        assert env.calls == []


def test_silently_dropped_file_is_not_recorded(env):
    """CLI exits 0 but never lists the file (e.g. RAW): must not be recorded as uploaded."""
    make_jpeg(env.root / "Pictures" / "ok.jpg", datetime(2019, 7, 4, 12, 0, 0))
    make_jpeg(env.root / "Pictures" / "IMG_20190704_120001_drop.jpg")

    def fake(paths):
        env.calls.append(list(paths))
        env.remote.extend(f"Nov 03 2026 15:56 1 KiB {os.path.basename(p)}"
                          for p in paths if "_drop" not in os.path.basename(p))
    env.proton.photo_upload = fake
    summary, up, failed = env.photos.sync_photos(env.db, apply=True)
    assert (up, failed) == (1, 1) and summary["not_in_photos_after_upload"] == 1
    assert [k[0] for k in env.db.photo_uploaded_keys()] == ["Pictures/ok.jpg"]
    assert env.photos.unconfirmed_path().read_text().strip() == "Pictures/IMG_20190704_120001_drop.jpg"


def test_verify_flags_recorded_but_missing_from_photos(env):
    make_jpeg(env.root / "Pictures" / "ok.jpg", datetime(2019, 7, 4, 12, 0, 0))
    env.photos.sync_photos(env.db, apply=True)
    env.remote.clear(); env.remote.append("Jan 01 2000 00:00 - unrelated_zz.jpg")
    r = env.photos.verify_photos(env.db)
    assert not r["clean"] and r["recorded_but_not_in_photos"] == 1


def test_raw_files_never_sent_but_listed(env):
    make_jpeg(env.root / "Pictures" / "ok.jpg", datetime(2019, 7, 4, 12, 0, 0))
    make_jpeg(env.root / "Pictures" / "DSC_3452.NEF", datetime(2022, 6, 10, 12, 0, 0))
    make_jpeg(env.root / "Pictures" / "DSC_3452.jpg", datetime(2022, 6, 10, 12, 0, 0))
    summary, up, failed = env.photos.sync_photos(env.db, apply=True)
    sent = sorted(os.path.basename(p) for c in env.calls for p in c)
    assert sent == ["DSC_3452.jpg", "ok.jpg"] and (up, failed) == (2, 0)
    assert summary["raw_not_uploaded"] == 1
    assert env.photos.raw_report_path().read_text().strip() == "Pictures/DSC_3452.NEF"


def test_converted_jpg_takes_date_from_raw_sibling(env):
    d = env.root / "Pictures" / "Animal party 2022"
    make_jpeg(d / "DSC_3452.jpg")                       # no EXIF date, mtime = conversion time
    (d / "DSC_3452.NEF").write_bytes(b"raw")
    make_jpeg(d / "DSC_9999.jpg")                       # no RAW sibling -> stays held back
    want = datetime(2022, 6, 10, 18, 30, 0).timestamp()
    env.photos._exiftool_dates = lambda paths: {p: want for p in paths if p.endswith(".NEF")}
    items, held = env.photos.split_held_back(env.photos.scan_photos())
    got = {os.path.basename(i.abs): i for i in items}
    assert got["DSC_3452.jpg"].date_source == "raw_sibling" and got["DSC_3452.jpg"].date == want
    assert got["DSC_3452.jpg"].needs_stage
    assert [os.path.basename(i.abs) for i in held] == ["DSC_9999.jpg"]
    assert (d / "DSC_3452.NEF").read_bytes() == b"raw"


def test_failed_batch_is_retried_file_by_file_and_names_the_bad_file(env):
    for n in ("a.jpg", "bad.jpg", "c.jpg"):
        make_jpeg(env.root / "Pictures" / n, datetime(2019, 7, 4, 12, 0, 0))
    calls = []

    def flaky(paths):
        calls.append([os.path.basename(p) for p in paths])
        for p in paths:                      # like the CLI: the good files go up, then it errors
            if not p.endswith("bad.jpg"):
                env.remote.append(f"Nov 03 2026 15:56 1 KiB {os.path.basename(p)}")
        if any(p.endswith("bad.jpg") for p in paths):
            raise env.proton.CliError("1 item(s) failed to upload")
        return ""
    env.proton.photo_upload = flaky
    summary, up, failed = env.photos.sync_photos(env.db, apply=True)
    assert (up, failed) == (2, 1)
    assert sorted(k[0] for k in env.db.photo_uploaded_keys()) == ["Pictures/a.jpg", "Pictures/c.jpg"]
    assert env.photos.unconfirmed_path().read_text().strip() == "Pictures/bad.jpg"
    assert calls[0] == ["a.jpg", "bad.jpg", "c.jpg"] and len(calls) == 4   # batch + 3 single retries
