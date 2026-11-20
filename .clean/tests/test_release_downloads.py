"""Download-channel identity, publication signatures, and retry boundaries."""

import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("clean_release_downloads", SCRIPTS / "release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
VERIFY_SPEC = importlib.util.spec_from_file_location("clean_verify_downloads", SCRIPTS / "verify-clean.py")
verify = importlib.util.module_from_spec(VERIFY_SPEC)
sys.path.insert(0, str(SCRIPTS))
try:
    VERIFY_SPEC.loader.exec_module(verify)
finally:
    sys.path.pop(0)


def public_key(value=0):
    key = base64.b64encode(b"Ed" + bytes([value]) * 40).decode("ascii")
    return base64.b64encode(f"untrusted comment: test public key\n{key}\n".encode()).decode("ascii")


class DownloadReleaseGates(unittest.TestCase):
    BASE = "https://downloads.example.com/cc-switch"
    GITHUB = "https://github.com/example/cc-switch/releases/latest/download/latest.json"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.start(patch.object(release, "STATE", self.state))
        self.settings = {"upstream_repository": "farion1231/cc-switch", "release_revision": 3, "require_signed_updates": True}
        self.start(patch.object(release, "config", return_value=self.settings))
        self.start(patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "example/cc-switch", "GITHUB_SHA": "a" * 40,
            "GH_TOKEN": "test-token", "TAURI_PUBLIC_KEY": public_key(), "HAS_SIGNING_KEY": "true",
            "CLEAN_UPDATE_ENDPOINT": self.GITHUB, "DOWNLOAD_BASE_URL": "",
            "DOWNLOAD_SSH_HOST": "mirror.example.com", "DOWNLOAD_SSH_PORT": "",
            "DOWNLOAD_SSH_USER": "downloads", "HAS_DOWNLOAD_SSH_KEY": "true",
            "HAS_DOWNLOAD_KNOWN_HOSTS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_OUTPUT": str(self.root / "outputs"), "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
            "TAURI_SIGNING_PRIVATE_KEY": "test-private-value", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "test-password",
        }))
        log = contextlib.redirect_stdout(io.StringIO())
        log.__enter__()
        self.addCleanup(log.__exit__, None, None, None)

    def start(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def data(self, base=""):
        endpoints = [base + "/stable/latest.json", self.GITHUB] if base else [self.GITHUB]
        return {
            "schema": 1, "repository": "example/cc-switch", "version": "4.0.36",
            "upstream_tag": "v4.0.36", "release_tag": "clean-v4.0.36-r3", "revision": 3,
            "upstream_sha": "b" * 40, "automation_sha": "a" * 40,
            "upstream_release_url": "https://github.com/farion1231/cc-switch/releases/tag/v4.0.36",
            "signed_updates": True, "updater_public_key": public_key(),
            "updater_endpoint": endpoints[0], "updater_endpoints": endpoints, "download_base_url": base,
        }

    def channel(self):
        return {"schema": 1, "application": "cc-switch", "repository": "example/cc-switch",
                "base_url": self.BASE, "updater_public_key": public_key(),
                "max_batch_bytes": 4 * 1024**3, "min_free_bytes": 2 * 1024**3}

    def upstream(self):
        return {"tag_name": "v4.0.36", "draft": False, "prerelease": False,
                "html_url": "https://github.com/farion1231/cc-switch/releases/tag/v4.0.36"}

    def mock_detect(self, *, existing=None):
        self.start(patch.object(release, "api", side_effect=[self.upstream(), {"private": False}]))
        self.start(patch.object(release, "find_release", return_value=existing))
        self.start(patch.object(release, "check_release_continuity"))
        self.start(patch.object(release, "resolve_commit", return_value="b" * 40))

    def test_base_url_is_canonical_https_public_dns_and_fixed_path(self):
        self.assertEqual(release.normalize_download_base("https://DOWNLOADS.example.com:443/cc-switch/"), self.BASE)
        self.assertEqual(release.normalize_download_base(""), "")
        bad = ["/", "////", "http://downloads.example.com/cc-switch", "https://user:pass@downloads.example.com/cc-switch",
               "https://downloads.example.com:8443/cc-switch", "https://downloads.example.com/other",
               self.BASE + "?query=1", self.BASE + "#fragment", self.BASE + "?", self.BASE + "#",
               "https://127.0.0.1/cc-switch", "https://[::1]/cc-switch", "https://localhost/cc-switch",
               "https://app.localhost/cc-switch", "https://app.local/cc-switch", "https://host/cc-switch", "https://-bad.example/cc-switch"]
        for value in bad:
            with self.subTest(url=value), self.assertRaises(SystemExit):
                release.normalize_download_base(value)

    def test_download_readiness_checks_server_identity_and_empty_port_defaults_to_22(self):
        with patch.object(release, "public_json", return_value=self.channel()) as fetch:
            release.check_download_readiness("example/cc-switch", public_key(), self.BASE)
        fetch.assert_called_once_with(self.BASE + "/channel.json", same_origin=True)

    def test_download_readiness_blocks_partial_credentials_before_network(self):
        for change in ({"HAS_DOWNLOAD_SSH_KEY": "false"}, {"HAS_DOWNLOAD_KNOWN_HOSTS": "false"},
                       {"DOWNLOAD_SSH_HOST": "-bad"}, {"DOWNLOAD_SSH_USER": "root;id"}, {"DOWNLOAD_SSH_PORT": "65536"}):
            with self.subTest(change=change), patch.dict(os.environ, change), patch.object(release, "public_json") as fetch:
                with self.assertRaises(SystemExit):
                    release.check_download_readiness("example/cc-switch", public_key(), self.BASE)
                fetch.assert_not_called()

    def test_download_readiness_rejects_identity_or_capacity_mismatches(self):
        changes = [{"schema": True}, {"application": "another-app"}, {"repository": "other/cc-switch"},
                   {"base_url": "https://elsewhere.example.com/cc-switch"}, {"updater_public_key": public_key(1)},
                   {"max_batch_bytes": 0}, {"max_batch_bytes": True}, {"min_free_bytes": 0}, {"min_free_bytes": -1}]
        for change in changes:
            with self.subTest(change=list(change)), patch.object(release, "public_json", return_value=dict(self.channel(), **change)):
                with self.assertRaises(SystemExit):
                    release.check_download_readiness("example/cc-switch", public_key(), self.BASE)

    def test_channel_preflight_does_not_follow_another_https_origin(self):
        response = SimpleNamespace(geturl=lambda: "https://other.example.com/channel.json", read=lambda n: b"{}")
        with patch.object(release.urllib.request, "urlopen", return_value=contextlib.nullcontext(response)):
            with self.assertRaisesRegex(RuntimeError, "outside its configured HTTPS origin"):
                release.public_json(self.BASE + "/channel.json", same_origin=True)

    def test_initial_unsigned_setup_stays_a_wait_before_mirror_preflight(self):
        with patch.dict(os.environ, {"TAURI_PUBLIC_KEY": "", "HAS_SIGNING_KEY": "false", "CLEAN_UPDATE_ENDPOINT": "",
                                    "DOWNLOAD_BASE_URL": self.BASE, "GITHUB_EVENT_NAME": "schedule"}):
            with patch.object(release, "has_signed_release_history", return_value=False), patch.object(release, "check_download_readiness") as ready:
                release.detect()
                ready.assert_not_called()
        self.assertEqual((self.root / "outputs").read_text(), "build=false\nstate=setup_required\n")

    def test_detect_configures_domain_priority_and_own_github_fallback(self):
        self.mock_detect()
        with patch.dict(os.environ, {"DOWNLOAD_BASE_URL": self.BASE + "/"}), patch.object(release, "public_json", return_value=self.channel()):
            release.detect()
        data = json.loads((self.state / "release.json").read_text())
        self.assertEqual(data["updater_endpoint"], self.BASE + "/stable/latest.json")
        self.assertEqual(data["updater_endpoints"], [self.BASE + "/stable/latest.json", self.GITHUB])
        self.assertEqual(data["download_base_url"], self.BASE)
        self.assertIn("download_enabled=true", (self.root / "outputs").read_text())

    def test_github_only_mode_does_not_require_download_setup(self):
        self.mock_detect()
        with patch.object(release, "check_download_readiness") as ready:
            release.detect()
        ready.assert_not_called()
        data = json.loads((self.state / "release.json").read_text())
        self.assertEqual(data["updater_endpoints"], [self.GITHUB])
        self.assertIn("download_enabled=false", (self.root / "outputs").read_text())

    def test_up_to_date_exports_retry_target_without_rebuild_or_domain_feed_check(self):
        self.mock_detect(existing={"draft": False})
        with patch.dict(os.environ, {"DOWNLOAD_BASE_URL": self.BASE}), patch.object(release, "public_json", return_value=self.channel()) as fetch:
            with patch.object(release, "verify_public_release") as github_check:
                release.detect()
        github_check.assert_called_once()
        fetch.assert_called_once_with(self.BASE + "/channel.json", same_origin=True)
        self.assertFalse((self.state / "release.json").exists())
        outputs = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
        self.assertEqual(outputs, {"build": "false", "state": "up_to_date", "release_tag": "clean-v4.0.36-r3",
                                   "signed_updates": "true", "download_enabled": "true"})

    def source(self):
        source = self.root / "source"
        (source / "src-tauri").mkdir(parents=True)
        release.write_json(source / "package.json", {"version": "4.0.36"})
        release.write_json(source / "src-tauri/tauri.conf.json", {
            "version": "4.0.36", "bundle": {"createUpdaterArtifacts": True},
            "plugins": {"updater": {"pubkey": "official", "endpoints": ["https://dl.ccswitch.io/latest.json"]},
                        "deep-link": {"desktop": {"schemes": ["ccswitch"]}}},
        })
        return source

    def test_native_configuration_and_verifier_accept_only_expected_endpoints_and_key(self):
        source, data = self.source(), self.data(self.BASE)
        with patch.object(release, "read_state", return_value=data):
            release.configure(source)
        conf = json.loads((source / "src-tauri/tauri.conf.json").read_text())
        self.assertEqual(conf["plugins"]["updater"]["endpoints"], data["updater_endpoints"])
        verify.verify_update_configuration(conf, data, "example/cc-switch")
        conf["plugins"]["updater"]["endpoints"].append("https://dl.ccswitch.io/latest.json")
        with self.assertRaisesRegex(RuntimeError, "endpoints differ"):
            verify.verify_update_configuration(conf, data)
        conf["plugins"]["updater"]["endpoints"] = data["updater_endpoints"]
        conf["plugins"]["updater"]["pubkey"] = public_key(1)
        with self.assertRaisesRegex(RuntimeError, "public key differs"):
            verify.verify_update_configuration(conf, data)

    def test_legacy_signed_github_provenance_remains_compatible(self):
        data = self.data()
        del data["updater_endpoints"], data["download_base_url"]
        self.assertEqual(release.expected_updater_endpoints(data), [self.GITHUB])

    def test_public_release_verification_is_github_only_even_for_domain_clients(self):
        data = self.data(self.BASE)
        fixed = "https://github.com/example/cc-switch/releases/download/" + data["release_tag"]
        manifest = {"version": data["version"], "platforms": {
            platform: {"url": f"{fixed}/{filename}", "signature": "fixture-signature"}
            for platform, filename in release.updater_files(data).items()
        }}
        with patch.object(release, "public_json", return_value=manifest) as fetch, patch.object(release, "public_asset_size", return_value=123):
            release.verify_public_release(data)
        self.assertEqual([entry.args[0] for entry in fetch.call_args_list], [fixed + "/latest.json", self.GITHUB])

    def previous(self, data):
        return {"tag_name": data["release_tag"], "assets": [{"name": "clean-build-provenance.json", "browser_download_url":
            "https://github.com/example/cc-switch/releases/download/" + data["release_tag"] + "/clean-build-provenance.json"}]}

    def test_used_domain_cannot_be_removed_or_changed_without_migration(self):
        old = self.data(self.BASE)
        for base in ("", "https://new.example.com/cc-switch"):
            with self.subTest(base=base), patch.object(release, "api", return_value=self.previous(old)), patch.object(release, "public_json", return_value=old):
                with self.assertRaisesRegex(SystemExit, "separate channel migration"):
                    release.check_release_continuity(self.data(base))

    def test_existing_github_clients_may_upgrade_to_first_domain_with_same_key(self):
        old = self.data()
        del old["download_base_url"], old["updater_endpoints"]
        with patch.object(release, "api", return_value=self.previous(old)), patch.object(release, "public_json", return_value=old):
            release.check_release_continuity(self.data(self.BASE))

    def test_checksum_signer_uses_pinned_cli_and_secret_env_then_verifies(self):
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "SHA256SUMS.txt").write_text("0" * 64 + "  example.bin\n")
        def sign(command, **kwargs):
            self.assertEqual(command[:7], ["npx", "--yes", "--package", "@tauri-apps/cli@2.12.4", "tauri", "signer", "sign"])
            self.assertNotIn("test-private-value", command)
            self.assertNotIn("test-password", command)
            self.assertEqual(kwargs["env"]["TAURI_SIGNING_PRIVATE_KEY"], "test-private-value")
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            (assets / "SHA256SUMS.txt.sig").write_text("base64-signature-fixture")
        with patch.object(release.subprocess, "run", side_effect=sign), patch.object(release, "verify_signatures") as check:
            release.sign_checksums(self.data(), assets)
        check.assert_called_once_with(self.data(), assets, {"SHA256SUMS.txt.sig"})

    def test_checksum_signer_failure_cannot_reuse_an_old_signature(self):
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "SHA256SUMS.txt").write_text("0" * 64 + "  example.bin\n")
        signature = assets / "SHA256SUMS.txt.sig"
        signature.write_text("stale-signature")
        with patch.object(release.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["npx"])), patch.object(release, "verify_signatures") as check:
            with self.assertRaisesRegex(SystemExit, "checksum signing failed"):
                release.sign_checksums(self.data(), assets)
        self.assertFalse(signature.exists())
        check.assert_not_called()

    def signed_assets(self):
        assets, data = self.root / "assets", self.data(self.BASE)
        assets.mkdir()
        source = self.state / "source.tar.gz"
        source.write_bytes(b"source fixture")
        data["source_archive_sha256"] = release.sha256(source)
        version = data["upstream_tag"]
        for platform in release.PLATFORMS:
            if platform == "macos":
                signed = [f"CC-Switch-{version}-macOS.tar.gz"]
                manual = [f"CC-Switch-{version}-macOS.zip", f"CC-Switch-{version}-macOS.dmg"]
            elif platform.startswith("windows"):
                suffix = "-arm64" if platform.endswith("arm64") else ""
                signed = [f"CC-Switch-{version}-Windows{suffix}.msi"]
                manual = [f"CC-Switch-{version}-Windows{suffix}-Portable.zip"]
            else:
                arch = "arm64" if platform.endswith("arm64") else "x86_64"
                signed = [f"CC-Switch-{version}-Linux-{arch}.{extension}" for extension in ("AppImage", "deb", "rpm")]
                manual = []
            names = signed + [name + ".sig" for name in signed] + manual
            for name in names:
                (assets / name).write_bytes(("fixture:" + name).encode())
            release.write_json(assets / f"build-{platform}.json", {"platform": platform,
                "source_archive_sha256": data["source_archive_sha256"], "assets": names, "macos_notarized": False})
        return assets, data

    def test_published_signature_and_checksum_set_are_complete_without_self_reference(self):
        assets, data = self.signed_assets()
        draft = {"id": 123, "draft": True, "tag_name": data["release_tag"], "target_commitish": data["automation_sha"]}
        def sign(_, directory):
            (directory / "SHA256SUMS.txt.sig").write_text("signed-checksum-fixture")
        def api(path, **kwargs):
            if path == "/repos/example/cc-switch":
                return {"private": False}
            return dict(draft, assets=[{"name": p.name, "size": p.stat().st_size}
                for p in assets.iterdir() if not p.name.startswith("build-")])
        with patch.object(release, "read_state", return_value=data), patch.object(release, "api", side_effect=api), patch.object(release, "find_release", return_value=draft):
            with patch.object(release, "verify_signatures"), patch.object(release, "check_release_continuity"), patch.object(release, "verify_public_release"):
                with patch.object(release, "sign_checksums", side_effect=sign) as signer, patch.object(release.subprocess, "run") as run:
                    release.publish(assets)
        signer.assert_called_once_with(data, assets)
        upload = next(item.args[0] for item in run.call_args_list if item.args[0][2] == "upload")
        uploaded = {Path(arg).name for arg in upload if str(arg).startswith(str(assets))}
        sums = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in (assets / "SHA256SUMS.txt").read_text().splitlines()}
        self.assertEqual(set(sums), uploaded - {"SHA256SUMS.txt", "SHA256SUMS.txt.sig"})
        provenance = json.loads((assets / "clean-build-provenance.json").read_text())
        self.assertEqual(provenance["asset_sha256"], {name: digest for name, digest in sums.items() if name != "clean-build-provenance.json"})
        for name, digest in sums.items():
            self.assertEqual(release.sha256(assets / name), digest)
        manifest = json.loads((assets / "latest.json").read_text())
        self.assertTrue(all(entry["url"].startswith("https://github.com/example/cc-switch/releases/download/clean-v4.0.36-r3/") for entry in manifest["platforms"].values()))
        self.assertIn("SHA256SUMS.txt.sig", uploaded)

    def test_checksum_signing_failure_blocks_every_github_write(self):
        assets, data = self.signed_assets()
        with patch.object(release, "read_state", return_value=data), patch.object(release, "api", return_value={"private": False}):
            with patch.object(release, "verify_signatures"), patch.object(release, "check_release_continuity"):
                with patch.object(release, "sign_checksums", side_effect=SystemExit("checksum rejected")), patch.object(release.subprocess, "run") as run:
                    with self.assertRaisesRegex(SystemExit, "checksum rejected"):
                        release.publish(assets)
                    run.assert_not_called()

    def test_missing_uploaded_checksum_signature_keeps_release_a_draft(self):
        assets, data = self.signed_assets()
        draft = {"id": 123, "draft": True, "tag_name": data["release_tag"], "target_commitish": data["automation_sha"]}
        def sign(_, directory):
            (directory / "SHA256SUMS.txt.sig").write_text("signed-checksum-fixture")
        def api(path, **kwargs):
            if path == "/repos/example/cc-switch":
                return {"private": False}
            return dict(draft, assets=[{"name": p.name, "size": p.stat().st_size}
                for p in assets.iterdir() if not p.name.startswith("build-") and p.name != "SHA256SUMS.txt.sig"])
        with patch.object(release, "read_state", return_value=data), patch.object(release, "api", side_effect=api), patch.object(release, "find_release", return_value=draft):
            with patch.object(release, "verify_signatures"), patch.object(release, "check_release_continuity"):
                with patch.object(release, "sign_checksums", side_effect=sign), patch.object(release.subprocess, "run") as run:
                    with self.assertRaisesRegex(SystemExit, "missing or stale attachments"):
                        release.publish(assets)
        self.assertEqual([item.args[0][2] for item in run.call_args_list], ["upload"])


if __name__ == "__main__":
    unittest.main()
