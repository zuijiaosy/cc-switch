"""Release gates: never ship a partial build or silently use the official updater."""

import base64
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

SPEC = importlib.util.spec_from_file_location("clean_release", Path(__file__).resolve().parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def public_key(value=0):
    key = base64.b64encode(b"Ed" + bytes([value]) * 40).decode("ascii")
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
        # Legacy manual-release fixtures remain useful for draft recovery gates.
        # Strict signed-mode behavior has dedicated tests below.
        self.settings = {"upstream_repository": "farion1231/cc-switch", "release_revision": 1, "require_signed_updates": False}
        self.config_patch = patch.object(release, "config", return_value=self.settings)
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.environment.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def upstream(self):
        return {"tag_name": "v4.0.35", "draft": False, "prerelease": False, "html_url": "https://github.com/farion1231/cc-switch/releases/tag/v4.0.35"}

    def data(self):
        return {"version": "4.0.35", "upstream_tag": "v4.0.35", "signed_updates": False, "updater_endpoint": "", "release_tag": "clean-v4.0.35-r1", "repository": "example/cc-switch", "revision": 1}

    def test_private_signed_release_is_rejected_before_build(self):
        with patch.dict(os.environ, {"TAURI_PUBLIC_KEY": public_key(), "HAS_SIGNING_KEY": "true"}):
            with patch.object(release, "find_release", return_value=None), patch.object(release, "api", side_effect=[self.upstream(), None]):
                with self.assertRaisesRegex(SystemExit, "publicly readable"):
                    release.detect()
        self.assertFalse((self.state / "release.json").exists())

    def test_partial_signing_configuration_is_not_silently_downgraded(self):
        with patch.dict(os.environ, {"TAURI_PUBLIC_KEY": public_key()}):
            with patch.object(release, "find_release", return_value=None), patch.object(release, "api", return_value=self.upstream()):
                with self.assertRaisesRegex(SystemExit, "Partial updater configuration"):
                    release.detect()

    def test_existing_published_release_is_not_rebuilt_or_overwritten(self):
        with patch.object(release, "find_release", return_value={"draft": False}), patch.object(release, "api", return_value=self.upstream()) as api:
            with patch.object(release, "output") as output:
                release.detect()
                output.assert_called_once_with("build", "false")
                self.assertEqual(api.call_count, 1)

    def test_draft_lookup_pages_releases_then_reads_canonical_id(self):
        tag = "clean-v4.0.35-r1"
        draft = {"id": 450838070, "tag_name": tag, "draft": True, "target_commitish": "a" * 40}
        first_page = [{"id": i + 1, "tag_name": f"v0.0.{i}"} for i in range(100)]
        with patch.object(release, "api", side_effect=[None, first_page, [draft], draft]) as api:
            self.assertEqual(release.find_release("example/cc-switch", tag), draft)
            self.assertEqual(api.call_args_list, [
                call(f"/repos/example/cc-switch/releases/tags/{tag}", allow_404=True),
                call("/repos/example/cc-switch/releases?per_page=100&page=1"),
                call("/repos/example/cc-switch/releases?per_page=100&page=2"),
                call("/repos/example/cc-switch/releases/450838070"),
            ])

    def test_release_lookup_returns_none_after_last_page(self):
        with patch.object(release, "api", side_effect=[None, []]) as api:
            self.assertIsNone(release.find_release("example/cc-switch", "missing"))
            self.assertEqual(api.call_count, 2)

    def test_published_release_lookup_uses_fast_tag_endpoint(self):
        published = {"id": 123, "tag_name": "clean-v4.0.35-r1", "draft": False}
        with patch.object(release, "api", return_value=published) as api:
            self.assertEqual(release.find_release("example/cc-switch", published["tag_name"]), published)
            api.assert_called_once_with("/repos/example/cc-switch/releases/tags/clean-v4.0.35-r1", allow_404=True)

    def prepared_assets(self):
        assets = self.root / "assets"
        assets.mkdir()
        source = self.state / "source.tar.gz"
        source.write_bytes(b"already verified prepared source archive")
        data = dict(self.data(), source_archive_sha256=release.sha256(source), automation_sha="a" * 40,
                    upstream_sha="b" * 40, upstream_release_url=self.upstream()["html_url"])
        for platform in release.PLATFORMS:
            name = f"asset-{platform}.bin"
            (assets / name).write_bytes(platform.encode())
            release.write_json(assets / f"build-{platform}.json", {
                "platform": platform, "source_archive_sha256": data["source_archive_sha256"],
                "assets": [name], "macos_notarized": False,
            })
        return assets, data

    def test_existing_draft_without_git_tag_resumes_by_target_commit(self):
        assets, data = self.prepared_assets()
        draft = {"id": 450838070, "draft": True, "tag_name": data["release_tag"], "target_commitish": data["automation_sha"]}
        def after_upload(path, **kwargs):
            self.assertEqual(path, "/repos/example/cc-switch/releases/450838070")
            return dict(draft, assets=[{"name": p.name, "size": p.stat().st_size}
                                       for p in assets.iterdir() if not p.name.startswith("build-")])
        with patch.object(release, "read_state", return_value=data), patch.object(release, "find_release", return_value=draft):
            with patch.object(release, "resolve_commit") as resolve, patch.object(release, "api", side_effect=after_upload):
                with patch.object(release.subprocess, "run") as run:
                    release.publish(assets)
                    resolve.assert_not_called()
                    commands = [item.args[0] for item in run.call_args_list]
                    self.assertEqual([cmd[2] for cmd in commands], ["upload", "edit"])
                    self.assertIn("--draft=false", commands[-1])

    def test_mismatched_draft_target_blocks_upload_and_publication(self):
        assets, data = self.prepared_assets()
        draft = {"id": 450838070, "draft": True, "target_commitish": "another-commit"}
        with patch.object(release, "read_state", return_value=data), patch.object(release, "find_release", return_value=draft):
            with patch.object(release.subprocess, "run") as run:
                with self.assertRaisesRegex(SystemExit, "another automation commit"):
                    release.publish(assets)
                run.assert_not_called()

    def test_incomplete_upload_keeps_existing_draft_unpublished(self):
        assets, data = self.prepared_assets()
        draft = {"id": 450838070, "draft": True, "tag_name": data["release_tag"], "target_commitish": data["automation_sha"]}
        with patch.object(release, "read_state", return_value=data), patch.object(release, "find_release", return_value=draft):
            with patch.object(release, "api", return_value=dict(draft, assets=[])), patch.object(release.subprocess, "run") as run:
                with self.assertRaisesRegex(SystemExit, "missing or stale attachments"):
                    release.publish(assets)
                self.assertEqual([item.args[0][2] for item in run.call_args_list], ["upload"])

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

    def signed_data(self):
        return dict(self.data(), revision=2, release_tag="clean-v4.0.35-r2", signed_updates=True,
                    updater_public_key=public_key(), updater_endpoint="https://github.com/example/cc-switch/releases/latest/download/latest.json")

    def previous_release(self, data):
        return {
            "tag_name": data["release_tag"],
            "assets": [{"name": "clean-build-provenance.json", "browser_download_url":
                        f"https://github.com/example/cc-switch/releases/download/{data['release_tag']}/clean-build-provenance.json"}],
        }

    def remote_manifest(self, data):
        base = f"https://github.com/{data['repository']}/releases/download/{data['release_tag']}"
        return {"version": data["version"], "platforms": {
            platform: {"url": f"{base}/{name}", "signature": "signed-test-fixture"}
            for platform, name in release.updater_files(data).items()
        }}

    def test_required_signing_never_falls_back_to_unsigned(self):
        self.settings["require_signed_updates"] = True
        with patch.object(release, "find_release", return_value=None), patch.object(release, "api", return_value=self.upstream()):
            with patch.object(release, "resolve_commit") as resolve:
                with self.assertRaisesRegex(SystemExit, "unsigned fallback is forbidden"):
                    release.detect()
                resolve.assert_not_called()

    def test_unsigned_r1_can_bootstrap_signed_r2_without_changing_app_version(self):
        old, new = self.data(), self.signed_data()
        with patch.object(release, "api", return_value=self.previous_release(old)), patch.object(release, "public_json", return_value=old):
            release.check_release_continuity(new)
        self.assertEqual(old["version"], new["version"])

    def test_unexpected_signing_key_rotation_is_blocked(self):
        old = self.signed_data()
        new = dict(old, version="4.0.36", upstream_tag="v4.0.36", release_tag="clean-v4.0.36-r2", updater_public_key=public_key(1))
        with patch.object(release, "api", return_value=self.previous_release(old)), patch.object(release, "public_json", return_value=old):
            with self.assertRaisesRegex(SystemExit, "differs from the previous signed release"):
                release.check_release_continuity(new)

    def test_signed_stream_cannot_downgrade_application_version(self):
        new = self.signed_data()
        old = dict(new, version="4.0.36", upstream_tag="v4.0.36", release_tag="clean-v4.0.36-r2")
        with patch.object(release, "api", return_value=self.previous_release(old)), patch.object(release, "public_json", return_value=old):
            with self.assertRaisesRegex(SystemExit, "older application version"):
                release.check_release_continuity(new)

    def test_signed_stream_accepts_new_official_version_with_same_key(self):
        old = self.signed_data()
        new = dict(old, version="4.0.36", upstream_tag="v4.0.36", release_tag="clean-v4.0.36-r2")
        with patch.object(release, "api", return_value=self.previous_release(old)), patch.object(release, "public_json", return_value=old):
            release.check_release_continuity(new)

    def test_public_update_check_recovers_from_temporary_cdn_lag(self):
        data = self.signed_data()
        manifest = self.remote_manifest(data)
        with patch.object(release, "public_json", side_effect=[RuntimeError("CDN not ready"), manifest, manifest]):
            with patch.object(release, "public_asset_size", return_value=1) as head, patch.object(release.time, "sleep") as sleep:
                release.verify_public_release(data)
                sleep.assert_called_once_with(3)
                self.assertEqual(head.call_count, 9)

    def test_public_manifest_cannot_redirect_clients_back_to_official_release(self):
        data = self.signed_data()
        manifest = self.remote_manifest(data)
        manifest["platforms"]["darwin-aarch64"]["url"] = "https://github.com/farion1231/cc-switch/releases/download/v4.0.35/official.tar.gz"
        with patch.object(release, "public_json", return_value=manifest), patch.object(release.time, "sleep") as sleep:
            with self.assertRaisesRegex(SystemExit, "outside this release"):
                release.verify_public_release(data)
            self.assertEqual(sleep.call_count, len(release.PUBLIC_CHECK_DELAYS) - 1)
            self.assertTrue(all(item.args[0] <= 60 for item in sleep.call_args_list))

    def test_already_published_signed_release_rechecks_feed_without_rebuild(self):
        self.settings.update(require_signed_updates=True, release_revision=2)
        with patch.dict(os.environ, {"TAURI_PUBLIC_KEY": public_key(), "HAS_SIGNING_KEY": "true"}):
            with patch.object(release, "api", side_effect=[self.upstream(), {"private": False}]), patch.object(release, "find_release", return_value={"draft": False}):
                with patch.object(release, "check_release_continuity"), patch.object(release, "verify_public_release") as verify:
                    with patch.object(release, "resolve_commit") as resolve, patch.object(release, "output") as output:
                        release.detect()
                        verify.assert_called_once()
                        resolve.assert_not_called()
                        output.assert_called_once_with("build", "false")

    def test_monthly_check_only_updates_status_file_with_existing_blob_lease(self):
        last = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
        current = {"sha": "c" * 40, "content": base64.b64encode(json.dumps({"checked_at": last.isoformat()}).encode()).decode()}
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "1234", "PREPARE_RESULT": "success"}):
            with patch.object(release, "api", side_effect=[{"private": False, "default_branch": "main"}, current]), patch.object(release.subprocess, "run") as run:
                release.maintain_schedule()
                command = run.call_args.args[0]
                self.assertEqual(command[:5], ["gh", "api", "--method", "PUT", "/repos/example/cc-switch/contents/.clean/check-state.json"])
                payload = json.loads((self.state / "check-state-update.json").read_text())
                self.assertEqual(payload["sha"], current["sha"])
                self.assertEqual(payload["branch"], "main")
                self.assertIn("[skip ci]", payload["message"])

    def test_recent_monthly_check_creates_no_commit(self):
        last = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)
        current = {"sha": "c" * 40, "content": base64.b64encode(json.dumps({"checked_at": last.isoformat()}).encode()).decode()}
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "1234"}):
            with patch.object(release, "api", side_effect=[{"private": False, "default_branch": "main"}, current]), patch.object(release.subprocess, "run") as run:
                release.maintain_schedule()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
