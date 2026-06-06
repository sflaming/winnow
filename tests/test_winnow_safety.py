import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from winnow import (
    PhotoDupeTUI,
    QUARANTINE_DIR_NAME,
    FileInfo,
    clear_exif_cache,
    find_content_matches,
    find_last_pending_tx,
    find_matches_hybrid,
    normalize_sd_stem_for_substring,
    full_hash_file,
    load_exif_for_fileinfo,
    load_recent_paths,
    partial_hash_file,
    paths_overlap,
    remember_recent_path,
    rename_file_replace_substring,
    scan_chunk_build_info,
    scan_directory_parallel_infos,
    save_recent_paths,
    select_folder_native,
    suggest_best_keep,
    validate_prefix,
)


class SafetyRulesTests(unittest.TestCase):
    def test_paths_overlap_detects_same_and_nested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drive = root / "drive"
            sd = drive / "sd"
            other = root / "other"
            drive.mkdir()
            sd.mkdir()
            other.mkdir()

            self.assertTrue(paths_overlap(drive, drive))
            self.assertTrue(paths_overlap(drive, sd))
            self.assertFalse(paths_overlap(drive, other))

    def test_validate_prefix_rejects_unsafe_values(self) -> None:
        self.assertIsNone(validate_prefix("COPIED_2026"))
        self.assertIsNotNone(validate_prefix("../bad"))
        self.assertIsNotNone(validate_prefix("bad/name"))
        self.assertIsNotNone(validate_prefix(r"bad\name"))

    def test_exact_match_requires_same_size(self) -> None:
        sd = FileInfo(
            path=Path("/sd/IMG_0001.JPG"),
            stem="IMG_0001",
            ext=".jpg",
            size=100,
            mtime=0,
            exif_dt=None,
            camera_model=None,
        )
        drv = FileInfo(
            path=Path("/drive/IMG_0001.JPG"),
            stem="IMG_0001",
            ext=".jpg",
            size=200,
            mtime=0,
            exif_dt=None,
            camera_model=None,
        )

        matches, _ = find_matches_hybrid(
            [sd],
            [drv],
            enable_substring_fallback=False,
            exif_tolerance_seconds=2,
        )
        self.assertEqual(matches, [])

    def test_blacklist_extension_can_still_exact_match(self) -> None:
        sd = FileInfo(
            path=Path("/sd/IMG_0001.XMP"),
            stem="IMG_0001",
            ext=".xmp",
            size=100,
            mtime=0,
            exif_dt=None,
            camera_model=None,
        )
        drv = FileInfo(
            path=Path("/drive/IMG_0001.XMP"),
            stem="IMG_0001",
            ext=".xmp",
            size=100,
            mtime=0,
            exif_dt=None,
            camera_model=None,
        )

        matches, _ = find_matches_hybrid(
            [sd],
            [drv],
            enable_substring_fallback=True,
            exif_tolerance_seconds=2,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].kind, "EXACT")

    def test_scan_includes_blacklisted_extensions_for_exact_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = root / "A.PNG"
            xmp = root / "A.XMP"
            png.write_bytes(b"png")
            xmp.write_bytes(b"xmp")

            infos = scan_chunk_build_info(([png, xmp],))
            exts = sorted(fi.ext for fi in infos)
            self.assertEqual(exts, [".png", ".xmp"])

    def test_suggest_best_keep_prefers_earliest_exif(self) -> None:
        sd = FileInfo(path=Path("/sd/a.jpg"), stem="a", ext=".jpg", size=1, mtime=0, exif_dt=100)
        drv = FileInfo(path=Path("/drv/a.jpg"), stem="a", ext=".jpg", size=1, mtime=0, exif_dt=120)
        keep_side, _ = suggest_best_keep(sd, drv)
        self.assertEqual(keep_side, "SD")

    def test_substring_fallback_not_blocked_when_sd_has_exif(self) -> None:
        sd = FileInfo(
            path=Path("/sd/IMG_0002.jpg"),
            stem="IMG_0002",
            ext=".jpg",
            size=100,
            mtime=0,
            exif_dt=1700000000,
        )
        drv = FileInfo(
            path=Path("/drv/IMG_0002_EDIT.jpg"),
            stem="IMG_0002_EDIT",
            ext=".jpg",
            size=99,
            mtime=0,
            exif_dt=None,
        )
        matches, _ = find_matches_hybrid(
            [sd],
            [drv],
            enable_substring_fallback=True,
            exif_tolerance_seconds=2,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].reason, "Substring stem match (legacy)")

    def test_substring_prefixed_sd_matches_when_scan_strip_token_set(self) -> None:
        sd = FileInfo(
            path=Path("/sd/COPIED_IMG_0002.jpg"),
            stem="COPIED_IMG_0002",
            ext=".jpg",
            size=100,
            mtime=0,
            exif_dt=None,
        )
        drv = FileInfo(
            path=Path("/drv/IMG_0002_EDIT.jpg"),
            stem="IMG_0002_EDIT",
            ext=".jpg",
            size=99,
            mtime=0,
            exif_dt=None,
        )
        matches, _ = find_matches_hybrid(
            [sd],
            [drv],
            enable_substring_fallback=True,
            exif_tolerance_seconds=0,
            sd_stem_strip_token="COPIED_",
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].reason, "Substring stem match (legacy)")

    def test_substring_prefixed_sd_no_match_without_scan_strip_token(self) -> None:
        sd = FileInfo(
            path=Path("/sd/COPIED_IMG_0002.jpg"),
            stem="COPIED_IMG_0002",
            ext=".jpg",
            size=100,
            mtime=0,
            exif_dt=None,
        )
        drv = FileInfo(
            path=Path("/drv/IMG_0002_EDIT.jpg"),
            stem="IMG_0002_EDIT",
            ext=".jpg",
            size=99,
            mtime=0,
            exif_dt=None,
        )
        matches, _ = find_matches_hybrid(
            [sd],
            [drv],
            enable_substring_fallback=True,
            exif_tolerance_seconds=0,
            sd_stem_strip_token="",
        )
        self.assertEqual(matches, [])

    def test_scan_strip_token_is_leading_once_and_case_sensitive(self) -> None:
        self.assertEqual(
            normalize_sd_stem_for_substring("COPIED_IMG_0001", "COPIED_"),
            "IMG_0001",
        )
        self.assertEqual(
            normalize_sd_stem_for_substring("COPIED_COPIED_IMG_0001", "COPIED_"),
            "COPIED_IMG_0001",
        )
        self.assertEqual(
            normalize_sd_stem_for_substring("IMG_COPIED_0001", "COPIED_"),
            "IMG_COPIED_0001",
        )
        self.assertEqual(
            normalize_sd_stem_for_substring("COPIED_IMG_0001", "copied_"),
            "COPIED_IMG_0001",
        )
        self.assertEqual(
            normalize_sd_stem_for_substring("COPIED_", "COPIED_"),
            "COPIED_",
        )

    def test_exif_time_is_not_used_for_matching(self) -> None:
        sd = FileInfo(
            path=Path("/sd/BURST_0001.jpg"),
            stem="BURST_0001",
            ext=".jpg",
            size=1000,
            mtime=0,
            exif_dt=1700000000,
            camera_model="Fujifilm X-T5",
        )
        drv = FileInfo(
            path=Path("/drv/BURST_0002.jpg"),
            stem="BURST_0002",
            ext=".jpg",
            size=1000,
            mtime=0,
            exif_dt=1700000000,
            camera_model="Fujifilm X-T5",
        )
        matches, _ = find_matches_hybrid(
            [sd],
            [drv],
            enable_substring_fallback=False,
            exif_tolerance_seconds=2,
        )
        self.assertEqual(matches, [])

    def test_partial_and_full_hash_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.bin"
            b = root / "b.bin"
            a.write_bytes(b"x" * 2048 + b"y")
            b.write_bytes(b"x" * 2048 + b"y")
            self.assertEqual(partial_hash_file(a), partial_hash_file(b))
            self.assertEqual(full_hash_file(a), full_hash_file(b))

    def test_rename_file_replace_substring_replaces_in_stem_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "COPIED_IMG_0001.jpg"
            p.write_bytes(b"x")
            ok, msg, dst = rename_file_replace_substring(p, "COPIED_", "DONE_")
            self.assertTrue(ok, msg)
            self.assertIsNotNone(dst)
            assert dst is not None
            self.assertEqual(dst.name, "DONE_IMG_0001.jpg")
            self.assertTrue(dst.exists())

    def test_rename_file_replace_substring_remove_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "COPIED_IMG_0001.jpg"
            p.write_bytes(b"x")
            ok, msg, dst = rename_file_replace_substring(p, "COPIED_", "")
            self.assertTrue(ok, msg)
            self.assertIsNotNone(dst)
            assert dst is not None
            self.assertEqual(dst.name, "IMG_0001.jpg")
            self.assertTrue(dst.exists())

    def test_rename_file_replace_substring_skips_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "IMG_0001.jpg"
            p.write_bytes(b"x")
            ok, msg, dst = rename_file_replace_substring(p, "COPIED_", "")
            self.assertFalse(ok)
            self.assertIn("SKIP", msg)
            self.assertIsNone(dst)
            self.assertTrue(p.exists())

    def test_find_last_pending_tx(self) -> None:
        records = [
            {"type": "move", "tx_id": "old", "src": "a", "dst": "b"},
            {"type": "undo_complete", "tx_id": "old"},
            {"type": "move", "tx_id": "new", "src": "c", "dst": "d"},
        ]
        tx_id, moves = find_last_pending_tx(records)
        self.assertEqual(tx_id, "new")
        self.assertEqual(len(moves), 1)

    def test_scan_falls_back_to_thread_pool_when_process_pool_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "A.PNG").write_bytes(b"png")
            with patch("winnow.ProcessPoolExecutor", side_effect=ValueError("bad value(s) in fds_to_keep")):
                infos = scan_directory_parallel_infos(root, "files")
            self.assertEqual(len(infos), 1)
            self.assertEqual(infos[0].ext, ".png")

    def test_select_folder_native_uses_osascript_on_macos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Proc:
                returncode = 0
                stdout = str(root) + "\n"

            with patch("winnow.sys.platform", "darwin"):
                with patch("winnow.subprocess.run", return_value=Proc()):
                    picked = select_folder_native("Pick folder", initial=root)
            self.assertEqual(picked, root)

    def test_select_folder_native_returns_none_on_cancel(self) -> None:
        class Proc:
            returncode = 1
            stdout = ""

        with patch("winnow.sys.platform", "darwin"):
            with patch("winnow.subprocess.run", return_value=Proc()):
                picked = select_folder_native("Pick folder")
        self.assertIsNone(picked)

    def test_recent_paths_round_trip_and_promote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "recent.json"
            recent = {"drive": [], "sd": []}
            remember_recent_path(recent, "drive", Path("/a"))
            remember_recent_path(recent, "drive", Path("/b"))
            remember_recent_path(recent, "drive", Path("/a"))
            self.assertEqual(recent["drive"][:2], ["/a", "/b"])
            save_recent_paths(recent, store)
            loaded = load_recent_paths(store)
            self.assertEqual(loaded["drive"][:2], ["/a", "/b"])

    def test_remember_recent_path_caps_at_limit(self) -> None:
        recent = {"drive": [], "sd": []}
        for i in range(20):
            remember_recent_path(recent, "sd", Path(f"/p{i}"), limit=5)
        self.assertEqual(len(recent["sd"]), 5)
        self.assertEqual(recent["sd"][0], "/p19")

    def test_scan_chunk_does_not_read_exif(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpg = root / "IMG_0001.jpg"
            jpg.write_bytes(b"fake-jpeg-data")

            infos = scan_chunk_build_info(([jpg],))
            self.assertEqual(len(infos), 1)
            fi = infos[0]
            self.assertIsNone(fi.exif_dt)
            self.assertIsNone(fi.camera_model)
            self.assertIsNone(fi.lens_model)
            self.assertIsNone(fi.width)
            self.assertIsNone(fi.height)

    def test_scan_excludes_quarantine_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / "keep.jpg"
            qdir = root / QUARANTINE_DIR_NAME
            qfile = qdir / "skip.jpg"
            keep.write_bytes(b"keep")
            qdir.mkdir()
            qfile.write_bytes(b"skip")

            infos = scan_directory_parallel_infos(root, "files")
            names = sorted(fi.path.name for fi in infos)
            self.assertEqual(names, ["keep.jpg"])

    def test_files_with_substring_in_name_matches_anywhere_in_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "A_COPIED_0001.jpg").write_bytes(b"x")
            (root / "B_0002.COPIED.jpg").write_bytes(b"x")
            (root / "sub").mkdir()
            (root / "sub" / "C_0003.jpg").write_bytes(b"x")

            app = PhotoDupeTUI()
            hits = app._files_with_substring_in_name(root, "COPIED")
            names = sorted(p.name for p in hits)
            self.assertEqual(names, ["A_COPIED_0001.jpg", "B_0002.COPIED.jpg"])

    def test_content_match_finds_renamed_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sd_file = root / "sd" / "IMG_0001.jpg"
            drv_file = root / "drive" / "RENAMED.jpg"
            sd_file.parent.mkdir(parents=True)
            drv_file.parent.mkdir(parents=True)
            content = b"identical-content-" * 100
            sd_file.write_bytes(content)
            drv_file.write_bytes(content)

            sd_fi = FileInfo(path=sd_file, stem="IMG_0001", ext=".jpg", size=len(content), mtime=0.0)
            drv_fi = FileInfo(path=drv_file, stem="RENAMED", ext=".jpg", size=len(content), mtime=0.0)

            matches = find_content_matches([sd_fi], [drv_fi], set())
            self.assertEqual(len(matches), 1)
            self.assertIn("Content hash match", matches[0].reason)

    def test_content_match_skips_already_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sd_file = root / "sd" / "IMG_0001.jpg"
            drv_file = root / "drive" / "RENAMED.jpg"
            sd_file.parent.mkdir(parents=True)
            drv_file.parent.mkdir(parents=True)
            content = b"identical-content-" * 100
            sd_file.write_bytes(content)
            drv_file.write_bytes(content)

            sd_fi = FileInfo(path=sd_file, stem="IMG_0001", ext=".jpg", size=len(content), mtime=0.0)
            drv_fi = FileInfo(path=drv_file, stem="RENAMED", ext=".jpg", size=len(content), mtime=0.0)

            matches = find_content_matches([sd_fi], [drv_fi], {sd_file})
            self.assertEqual(len(matches), 0)

    def test_content_match_different_content_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sd_file = root / "sd" / "IMG_0001.jpg"
            drv_file = root / "drive" / "OTHER.jpg"
            sd_file.parent.mkdir(parents=True)
            drv_file.parent.mkdir(parents=True)
            # Same size but different content
            sd_file.write_bytes(b"a" * 2000)
            drv_file.write_bytes(b"b" * 2000)

            sd_fi = FileInfo(path=sd_file, stem="IMG_0001", ext=".jpg", size=2000, mtime=0.0)
            drv_fi = FileInfo(path=drv_file, stem="OTHER", ext=".jpg", size=2000, mtime=0.0)

            matches = find_content_matches([sd_fi], [drv_fi], set())
            self.assertEqual(len(matches), 0)


if __name__ == "__main__":
    unittest.main()
