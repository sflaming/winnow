import tempfile
import unittest
from pathlib import Path

from photo_dupe import (
    FileInfo,
    find_matches_hybrid,
    paths_overlap,
    scan_chunk_build_info,
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

    def test_blacklist_extension_is_ignored_in_matching(self) -> None:
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
        self.assertEqual(matches, [])

    def test_scan_skips_blacklisted_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = root / "A.PNG"
            xmp = root / "A.XMP"
            png.write_bytes(b"png")
            xmp.write_bytes(b"xmp")

            infos = scan_chunk_build_info(([png, xmp],))
            exts = sorted(fi.ext for fi in infos)
            self.assertEqual(exts, [".png"])


if __name__ == "__main__":
    unittest.main()
