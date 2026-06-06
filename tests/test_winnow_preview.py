import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from winnow import (
    FileInfo,
    _preview_cache_path_for,
    clear_exif_cache,
    load_exif_for_fileinfo,
    resolve_preview_image,
)


class PreviewHelpersTests(unittest.TestCase):
    def test_preview_cache_path_is_stable_for_same_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "img.raf"
            raw.write_bytes(b"raw-data")
            a = _preview_cache_path_for(raw)
            b = _preview_cache_path_for(raw)
            self.assertEqual(a, b)
            self.assertEqual(a.suffix.lower(), ".jpg")

    def test_resolve_preview_image_extracts_raf_with_exiftool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "img.raf"
            raw.write_bytes(b"raw")

            class Proc:
                returncode = 0
                stdout = b"jpeg-bytes"
                stderr = b""

            with patch("winnow.shutil.which", side_effect=lambda name: "/usr/bin/exiftool" if name == "exiftool" else None):
                with patch("winnow.subprocess.run", return_value=Proc()):
                    preview, note = resolve_preview_image(raw)

            self.assertIsNotNone(preview)
            assert preview is not None
            self.assertTrue(preview.exists())
            self.assertEqual(preview.read_bytes(), b"jpeg-bytes")
            self.assertIn("embedded preview", note)

    def test_resolve_preview_image_raf_reports_backend_hint_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "img.raf"
            raw.write_bytes(b"raw")

            with patch("winnow.shutil.which", return_value=None):
                preview, note = resolve_preview_image(raw)

            self.assertIsNone(preview)
            self.assertIn("install exiftool or rawpy+Pillow", note)

    def test_load_exif_for_fileinfo_populates_exif_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpg = Path(tmp) / "img.jpg"
            jpg.write_bytes(b"fake-jpg")

            fi = FileInfo(path=jpg, stem="img", ext=".jpg", size=8, mtime=0.0)
            clear_exif_cache()

            with patch("winnow.read_exif_quick", return_value=(1700000000, "Canon EOS R5", "RF 50mm", 6000, 4000)):
                result = load_exif_for_fileinfo(fi)

            self.assertEqual(result.exif_dt, 1700000000)
            self.assertEqual(result.camera_model, "Canon EOS R5")
            self.assertEqual(result.lens_model, "RF 50mm")
            self.assertEqual(result.width, 6000)
            self.assertEqual(result.height, 4000)

    def test_load_exif_for_fileinfo_skips_non_exif_ext(self) -> None:
        fi = FileInfo(path=Path("/fake/img.png"), stem="img", ext=".png", size=100, mtime=0.0)
        clear_exif_cache()
        result = load_exif_for_fileinfo(fi)
        self.assertIs(result, fi)
        self.assertIsNone(result.exif_dt)

    def test_clear_exif_cache_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpg = Path(tmp) / "img.jpg"
            jpg.write_bytes(b"fake")

            fi = FileInfo(path=jpg, stem="img", ext=".jpg", size=4, mtime=0.0)
            clear_exif_cache()

            with patch("winnow.read_exif_quick", return_value=(100, None, None, None, None)):
                r1 = load_exif_for_fileinfo(fi)
            self.assertEqual(r1.exif_dt, 100)

            clear_exif_cache()

            with patch("winnow.read_exif_quick", return_value=(200, None, None, None, None)):
                r2 = load_exif_for_fileinfo(fi)
            self.assertEqual(r2.exif_dt, 200)


if __name__ == "__main__":
    unittest.main()
