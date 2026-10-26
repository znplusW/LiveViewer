"""Keep release notices tied to real wheel paths and loaded decoder versions."""
from email.message import Message
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import collect_licenses
from scripts.collect_licenses import heif_decoder_sources


class LicenseAttributionTests(unittest.TestCase):
    def test_normalized_distribution_name_does_not_change_notice_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "wheel"
            destination = Path(temporary) / "collected"
            license_path = Path("pi_heif-1.4.0.dist-info/licenses/LICENSES_bundled.txt")
            original = b"Original wheel source link: libde265/v1.0.15\r\n"
            (wheel / license_path).parent.mkdir(parents=True)
            (wheel / license_path).write_bytes(original)
            metadata = Message()
            metadata["Name"] = "pi_heif"
            distribution = SimpleNamespace(metadata=metadata, version="1.4.0",
                                           files=[license_path], locate_file=lambda path: wheel / path)
            info = {"libheif": "1.23.0", "decoders": {
                "libde265": "libde265 HEVC decoder, version 1.1.1"}}
            with (patch.object(collect_licenses, "PACKAGES", ("pi-heif",)),
                  patch.object(collect_licenses, "copy_exiftool_notices", return_value={"version": "13.38"}),
                  patch.object(collect_licenses.metadata, "distribution", return_value=distribution),
                  patch.object(collect_licenses.sysconfig, "get_paths", return_value={
                      "purelib": str(wheel), "platlib": str(wheel)}),
                  patch("pi_heif.libheif_info", return_value=info),
                  patch.object(collect_licenses.urllib.request, "urlopen",
                               side_effect=lambda *args, **kwargs: io.BytesIO(b"Upstream notice\n"))):
                manifest = collect_licenses.gather(destination)
            notice = (destination / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
            line = next(line for line in notice.splitlines() if line.startswith("- pi_heif "))
            stated_directory = line.split("original notices in ", 1)[1].removesuffix("/.")
            self.assertTrue((destination / stated_directory).is_dir(), line)
            copied = destination / manifest["distributions"][0]["original_license_files"][0]
            self.assertEqual(copied.read_bytes(), original)
            self.assertEqual((wheel / license_path).read_bytes(), original)
            self.assertIn("https://github.com/strukturag/libde265/tree/v1.1.1", notice)
            self.assertEqual(manifest["pi_heif_decoder_sources"]["libde265"]["version"], "1.1.1")

    def test_windows_decoder_sources_follow_actual_versions(self):
        sources = heif_decoder_sources({
            "libheif": "1.23.0",
            "decoders": {"libde265": "libde265 HEVC decoder, version 1.1.1"},
        })
        self.assertEqual(sources["libheif"]["source"],
                         "https://github.com/strukturag/libheif/tree/v1.23.0")
        self.assertEqual(sources["libde265"]["source"],
                         "https://github.com/strukturag/libde265/tree/v1.1.1")
        self.assertEqual(sources["libde265"]["license_source"],
                         "https://raw.githubusercontent.com/strukturag/libde265/v1.1.1/COPYING")
        self.assertNotIn("1.18.1", str(sources))
        self.assertNotIn("1.0.15", str(sources))

    def test_unknown_decoder_version_does_not_emit_guessed_source_links(self):
        for info in ({}, {"libheif": "1.23.0", "decoders": {"libde265": "unknown"}}):
            with self.subTest(info=info), self.assertRaisesRegex(RuntimeError, "decoder versions"):
                heif_decoder_sources(info)


if __name__ == "__main__":
    unittest.main()
