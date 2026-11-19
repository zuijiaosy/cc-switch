"""Release gates: never ship a partial build or silently use the official updater."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("clean_release", Path(__file__).resolve().parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def public_key():
    key = base64.b64encode(b"Ed" + bytes(40)).decode("ascii")
    return base64.b64encode(f"untrusted comment: test public key\n{key}\n".encode()).decode("ascii")


class ReleaseGates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.state_patch = patch.object(release, "STATE", self.state)
        self.state_patch.start()
        self.environment = patch.dict(os.environ, {
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "example/cc-switch",
            "GITHUB_SHA": "a" * 40,
            "TAURI_PUBLIC_KEY": "",
            "CLEAN_UPDATE_ENDPOINT": "",
            "HAS_SIGNING_KEY": "false",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def upstream(self):
        return {"tag_name": "v4.0.35", "draft": False, "prerelease": False, "html_url": "https://github.com/farion1231/cc-switch/releases/tag/v4.0.35"}

    def data(self):
        return {"version": "4.0.35", "upstream_tag": "v4.0.35", "signed_updates": False, "updater_endpoint": "", "release_tag": "clean-v4.0.35-r1", "repository": "example/cc-switch", "revision": 1}

    def test_private_signed_release_is_rejected_before_build(self):
        with patch.dict(os.environ, {"TAURI_PUBLIC_KEY": public_key(), "HAS_SIGNING_KEY": "true"}):
            with patch.object(release, "api", side_effect=[self.upstream(), None, None]):
                with self.assertRaisesRegex(SystemExit, "publicly readable"):
                    release.detect()
        self.assertFalse((self.state / "release.json").exists())

    def test_partial_signing_configuration_is_not_silently_downgraded(self):
        with patch.dict(os.environ, {"TAURI_PUBLIC_KEY": public_key()}):
            with patch.object(release, "api", side_effect=[self.upstream(), None]):
                with self.assertRaisesRegex(SystemExit, "Partial updater configuration"):
                    release.detect()

    def test_existing_published_release_is_not_rebuilt_or_overwritten(self):
        with patch.object(release, "api", side_effect=[self.upstream(), {"draft": False}]) as api:
            with patch.object(release, "output") as output:
                release.detect()
                output.assert_called_once_with("build", "false")
                self.assertEqual(api.call_count, 2)

    def test_manual_mode_removes_all_official_update_endpoints(self):
        source = self.root / "source"
        (source / "src-tauri").mkdir(parents=True)
        release.write_json(source / "package.json", {"version": "4.0.35"})
        release.write_json(source / "src-tauri/tauri.conf.json", {
            "version": "4.0.35", "identifier": "com.ccswitch.desktop", "bundle": {"createUpdaterArtifacts": True},
            "plugins": {"updater": {"pubkey": "original", "endpoints": ["https://dl.ccswitch.io/latest.json", "https://github.com/farion1231/cc-switch/releases/latest/download/latest.json"]}, "deep-link": {"desktop": {"schemes": ["ccswitch"]}}},
        })
        with patch.object(release, "read_state", return_value=self.data()):
            release.configure(source)
        conf = json.loads((source / "src-tauri/tauri.conf.json").read_text())
        self.assertEqual(conf["plugins"]["updater"]["endpoints"], [])
        self.assertFalse(conf["bundle"]["createUpdaterArtifacts"])
        self.assertEqual(conf["plugins"]["deep-link"]["desktop"]["schemes"], ["ccswitch"])
        self.assertEqual(conf["identifier"], "com.ccswitch.desktop")

    def test_missing_platform_blocks_publication_before_network_writes(self):
        assets = self.root / "assets"
        assets.mkdir()
        with patch.object(release, "read_state", return_value=self.data()):
            with patch.object(release, "api") as api, patch.object(release.subprocess, "run") as run:
                with self.assertRaisesRegex(SystemExit, "Missing platform"):
                    release.publish(assets)
                api.assert_not_called()
                run.assert_not_called()

    def test_incomplete_updater_manifest_is_rejected(self):
        assets = self.root / "assets"
        assets.mkdir()
        with self.assertRaisesRegex(SystemExit, "Missing updater artifact"):
            release.make_manifest(self.data(), assets)

    def test_invalid_signature_format_never_reaches_signature_verifier(self):
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "app.msi.sig").write_text("not-base64!")
        data = dict(self.data(), updater_public_key=public_key())
        with patch.object(release.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "Malformed updater signature"):
                release.verify_signatures(data, assets, {"app.msi.sig"})
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
