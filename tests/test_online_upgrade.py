import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request
import zipfile

from django.test import SimpleTestCase

from scripts import verify_online_upgrade as verify


class OnlineUpgradeVerifierTests(SimpleTestCase):
    def _archive(self, folder: Path, version: str) -> tuple[Path, dict]:
        payload = ("fixture-" + version).encode()
        manifest = {
            "schema": 1,
            "version": version,
            "files": {"fixture.txt": hashlib.sha256(payload).hexdigest()},
        }
        path = folder / (version + ".zip")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("fixture.txt", payload)
        return path, manifest

    def test_parser_accepts_ci_archive_interface(self):
        args = verify.build_parser().parse_args([
            "--bridge", "桥接 包.zip", "--modern", "现代 包.zip",
            "--transport", "fixture", "--provenance", "fixture",
        ])
        self.assertEqual(args.bridge, Path("桥接 包.zip"))
        self.assertEqual(args.modern, Path("现代 包.zip"))
        self.assertEqual(args.transport, "fixture")
        self.assertEqual(args.provenance, "fixture")

    def test_fixture_transport_serves_release_list_and_archives(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            bridge_path, bridge_manifest = self._archive(folder, "2026.09.23.0")
            modern_path, modern_manifest = self._archive(folder, "2026.09.23.1")
            bridge = verify._artifact(101, bridge_path, bridge_manifest)
            modern = verify._artifact(102, modern_path, modern_manifest)
            with verify.FixtureServer(bridge, modern) as fixture:
                latest = json.load(urllib.request.urlopen(fixture.api + "/releases/latest"))
                listing = json.load(urllib.request.urlopen(fixture.api + "/releases?per_page=100"))
                with urllib.request.urlopen(fixture.api + "/releases/assets/102") as response:
                    downloaded = response.read()
            self.assertEqual(latest["tag_name"], "v2026.09.23.0")
            self.assertEqual([item["tag_name"] for item in listing], ["v2026.09.23.1", "v2026.09.23.0"])
            self.assertEqual(downloaded, modern_path.read_bytes())

    def test_sentinel_comparison_allows_additive_columns(self):
        before = {"upload": {"columns": ["id", "sha256"], "values": ["u1", "abc"]}}
        after = {"upload": {"columns": ["id", "sha256", "new_field"], "values": ["u1", "abc", 0]}}
        verify._compare_rows(before, after)
        after["upload"]["values"][1] = "changed"
        with self.assertRaises(verify.VerificationError):
            verify._compare_rows(before, after)
