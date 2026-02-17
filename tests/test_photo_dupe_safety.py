import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_dupe import (
    FileInfo,
    find_last_pending_tx,
    find_matches_hybrid,
    full_hash_file,
    load_recent_paths,
    partial_hash_file,
    paths_overlap,
    remember_recent_path,
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

    def test_partial_and_full_hash_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.bin"
            b = root / "b.bin"
            a.write_bytes(b"x" * 2048 + b"y")
            b.write_bytes(b"x" * 2048 + b"y")
            self.assertEqual(partial_hash_file(a), partial_hash_file(b))
            self.assertEqual(full_hash_file(a), full_hash_file(b))

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
            with patch("photo_dupe.ProcessPoolExecutor", side_effect=ValueError("bad value(s) in fds_to_keep")):
                infos = scan_directory_parallel_infos(root, "files")
            self.assertEqual(len(infos), 1)
            self.assertEqual(infos[0].ext, ".png")

    def test_select_folder_native_uses_osascript_on_macos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Proc:
                returncode = 0
                stdout = str(root) + "\n"

            with patch("photo_dupe.sys.platform", "darwin"):
                with patch("photo_dupe.subprocess.run", return_value=Proc()):
                    picked = select_folder_native("Pick folder", initial=root)
            self.assertEqual(picked, root)

    def test_select_folder_native_returns_none_on_cancel(self) -> None:
        class Proc:
            returncode = 1
            stdout = ""

        with patch("photo_dupe.sys.platform", "darwin"):
            with patch("photo_dupe.subprocess.run", return_value=Proc()):
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


if __name__ == "__main__":
    unittest.main()
