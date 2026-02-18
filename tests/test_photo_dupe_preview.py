import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from photo_dupe import (
    _preview_cache_path_for,
    render_preview_ascii,
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

            with patch("photo_dupe.shutil.which", side_effect=lambda name: "/usr/bin/exiftool" if name == "exiftool" else None):
                with patch("photo_dupe.subprocess.run", return_value=Proc()):
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

            with patch("photo_dupe.shutil.which", return_value=None):
                preview, note = resolve_preview_image(raw)

            self.assertIsNone(preview)
            self.assertIn("install exiftool or rawpy+Pillow", note)

    def test_render_preview_ascii_reports_missing_chafa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpg = Path(tmp) / "img.jpg"
            jpg.write_bytes(b"jpg")

            def fake_which(name: str):
                if name == "chafa":
                    return None
                return "/usr/bin/exiftool"

            with patch("photo_dupe.shutil.which", side_effect=fake_which):
                art, note = render_preview_ascii(jpg)

            self.assertIsNone(art)
            self.assertIn("chafa unavailable", note)

    def test_render_preview_ascii_reports_chafa_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpg = Path(tmp) / "img.jpg"
            jpg.write_bytes(b"jpg")

            with patch("photo_dupe.shutil.which", side_effect=lambda name: "/usr/bin/chafa" if name == "chafa" else None):
                with patch("photo_dupe.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["chafa"], timeout=1)):
                    art, note = render_preview_ascii(jpg)

            self.assertIsNone(art)
            self.assertIn("timeout", note)


if __name__ == "__main__":
    unittest.main()
