"""Offline HTTP/SFTP boundary tests plus real detached-signature fixtures."""

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("sync_downloads", Path(__file__).resolve().parents[1] / "scripts/sync-downloads.py")
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

# Public key/signature for an isolated test fixture. No private signing key is
# checked in, and this key is unrelated to any release or user's credentials.
PUBLIC_KEY = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXkgNjAyMDlGQjgxRkMxMzE3NwpSV1IzTWNFZnVKOGdZSzBOZ2w2UFdpU1A3Nzl1NCtLNUNMbml4MWM4MjBhM21yOVdVc1NOQ2E4awo="
SIGNATURE = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIG1pbmlzaWduIHNlY3JldCBrZXkKUlVSM01jRWZ1SjhnWU5FY1pPNHErNWxuZmRud09pUEVlbUt3d1NNZVUyeUc0NkpQc3lYc2dvbnM2bW5yaldXY2MrSHFGNjdFV0JabFZRdGFNUTA5VzQ0MjVjeTlZM2R4bndBPQp0cnVzdGVkIGNvbW1lbnQ6IENDIFN3aXRjaCB1bml0IHRlc3QgZml4dHVyZQpFRzZOY2FYVVROL3R3Ry8zRHJXYmxwVEEyZFRIQWFHbllKUUoyakhFL1NMeEozK1VHNXlFSkI4UHBXOWFRbE9DYlRrUWY5NjBodWlyS0Jzb3RUL2JCQT09Cg=="
SIGNED_PAYLOAD = b"CC Switch signed checksum fixture\n"
HOST_KEY = base64.b64encode(b"host-key-fixture-material-for-mocked-sftp").decode()
SFTP_SERVER = next((path for path in ("/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server",
                                    "/usr/libexec/sftp-server") if Path(path).is_file()), None)


def encoded(value):
    return (json.dumps(value, sort_keys=True) + "\n").encode()


class Response(io.BytesIO):
    def __init__(self, url, data=b"", status=200, headers=None):
        super().__init__(data)
        self.url = url
        self.status = status
        self.headers = headers or {}

    def geturl(self):
        return self.url


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = io.StringIO()
        capture = contextlib.redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.settings = sync.Settings("example/cc-switch", "clean-v4.0.35-r2", "unit-test-api-token", PUBLIC_KEY,
                                      "https://downloads.example/cc-switch", "upload.example", 22, "ccswitch_upload",
                                      "TEST PRIVATE KEY THAT MUST NEVER BE PRINTED", f"upload.example ssh-ed25519 {HOST_KEY}\n")

    def bundle(self, settings=None):
        settings = settings or self.settings
        version, revision, _ = sync.parse_tag(settings.tag)
        files = {name: (name.encode() + b"\n" + b"A" * 2048)
                 for name in sync.expected_assets(version) - {sync.SUMS, sync.SUMS_SIG, sync.PROVENANCE, "latest.json"}}
        for name in files:
            if name.endswith(".sig"):
                files[name] = SIGNATURE.encode() + b"\n"
        manifest = {"version": version, "notes": "Fixture", "pub_date": "2026-11-20T00:00:00Z", "platforms": {
            platform: {"url": f"https://github.com/{settings.repository}/releases/download/{settings.tag}/{name}", "signature": SIGNATURE}
            for platform, name in sync.expected_updates(version).items()}}
        files["latest.json"] = encoded(manifest)
        provenance = {"schema": 1, "repository": settings.repository, "release_tag": settings.tag,
                      "version": version, "revision": revision, "signed_updates": True, "updater_public_key": PUBLIC_KEY,
                      "asset_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        files[sync.PROVENANCE] = encoded(provenance)
        files[sync.SUMS] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items())).encode()
        files[sync.SUMS_SIG] = SIGNATURE.encode() + b"\n"
        assets = [{"name": name, "state": "uploaded", "size": len(data),
                   "browser_download_url": f"https://github.com/{settings.repository}/releases/download/{settings.tag}/{name}"}
                  for name, data in files.items()]
        metadata = {"tag_name": settings.tag, "draft": False, "prerelease": False,
                    "published_at": "2026-11-20T00:00:00Z", "assets": assets}
        return files, metadata, manifest

    def release(self):
        files, _, manifest = self.bundle()
        directory = self.root / "assets"
        directory.mkdir()
        for name, data in files.items():
            (directory / name).write_bytes(data)
        return sync.VerifiedRelease(self.settings, directory, {name: len(data) for name, data in files.items()},
                                    {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}, manifest)

    def http_fixture(self, files, metadata):
        http = Mock()
        http.small.return_value = encoded(metadata)
        def stream(url, size, *, destination, **kwargs):
            data = files[url.rsplit("/", 1)[1]]
            self.assertEqual(len(data), size)
            destination.write_bytes(data)
            return hashlib.sha256(data).hexdigest()
        http.stream.side_effect = stream
        return http

    def receipt(self, release, status="published", **extra):
        return {"schema": 1, "repository": self.settings.repository, "release_tag": self.settings.tag,
                "checksums_sha256": release.checksums_hash, "status": status, **extra}

    def test_tag_and_ssh_values_reject_path_and_command_injection(self):
        for tag in ("clean-v01.2.3-r2", "clean-v1.2.3-r0", "clean-v1.2.3-r2/../x", "clean-v1.2.3-r2\nput evil"):
            with self.subTest(tag=tag), self.assertRaises(sync.SyncError):
                sync.parse_tag(tag)
        for host in ("evil@host", "-oProxyCommand=bad", "host:22", "host\nput", "../host"):
            with self.subTest(host=host), self.assertRaises(sync.SyncError):
                sync.validate_ssh(host, "22", "uploader", self.settings.known_hosts)
        with self.assertRaises(sync.SyncError):
            sync.validate_ssh("upload.example", "65536", "uploader", self.settings.known_hosts)

    def test_known_hosts_requires_only_the_exact_host_port(self):
        for hosts in ("*", "|1|hashed|host", "other.example", "upload.example,other.example", "@cert-authority"):
            with self.subTest(hosts=hosts), self.assertRaises(sync.SyncError):
                sync.validate_ssh("upload.example", "22", "uploader", f"{hosts} ssh-ed25519 {HOST_KEY}")
        with self.assertRaises(sync.SyncError):
            sync.validate_ssh("upload.example", "2222", "uploader", self.settings.known_hosts)
        self.assertEqual(sync.validate_ssh("upload.example", "2222", "uploader", f"[upload.example]:2222 ssh-ed25519 {HOST_KEY}")[1], 2222)
        # Non-zero base64 pad bits can decode successfully but are not the
        # canonical host-key encoding accepted by the owner setup helper.
        noncanonical = base64.b64encode(b"A" * 16).decode()[:-3] + "R=="
        with self.assertRaisesRegex(sync.SyncError, "Invalid SSH host key"):
            sync.validate_ssh("upload.example", "22", "uploader", f"upload.example ssh-ed25519 {noncanonical}")

    def test_base_url_is_a_public_dns_https_channel_with_standard_port(self):
        self.assertEqual(sync.base_url("https://Downloads.Example.com:443/cc-switch/"), "https://downloads.example.com/cc-switch")
        for url in ("https://localhost/cc-switch", "https://files.local/cc-switch", "https://127.0.0.1/cc-switch",
                    "https://0x7f.0.0.1/cc-switch", "https://2130706433/cc-switch", "https://[::1]/cc-switch",
                    "https://files.example.com:8443/cc-switch", "https://files.example.com/cc-switch//",
                    "https://user@files.example.com/cc-switch", "https://files.example.com/cc-switch?token=bad",
                    "https://files.example.com/cc-switch#fragment", "http://files.example.com/cc-switch"):
            with self.subTest(url=url), self.assertRaises(sync.SyncError):
                sync.base_url(url)

    def test_empty_actions_ssh_port_uses_the_default_port(self):
        env = {"GH_TOKEN": self.settings.token, "GITHUB_REPOSITORY": self.settings.repository,
               "TAURI_PUBLIC_KEY": PUBLIC_KEY, "DOWNLOAD_BASE_URL": self.settings.base,
               "DOWNLOAD_SSH_HOST": self.settings.host, "DOWNLOAD_SSH_PORT": "", "DOWNLOAD_SSH_USER": self.settings.user,
               "DOWNLOAD_SSH_PRIVATE_KEY": self.settings.private_key, "DOWNLOAD_SSH_KNOWN_HOSTS": self.settings.known_hosts}
        self.assertEqual(sync.Settings.from_env(self.settings.tag, env).port, 22)

    def test_channel_matches_repository_base_key_and_numeric_limits(self):
        channel = {"schema": 1, "application": "cc-switch", "repository": self.settings.repository,
                   "base_url": self.settings.base, "updater_public_key": PUBLIC_KEY,
                   "max_batch_bytes": 10**9, "min_free_bytes": 10**8}
        sync.validate_channel(channel, self.settings)
        for change in ({"repository": "other/repo"}, {"base_url": "https://evil/cc-switch"},
                       {"max_batch_bytes": True}, {"min_free_bytes": 0}, {"min_free_bytes": -1}, {"schema": True}):
            with self.subTest(change=change), self.assertRaises(sync.SyncError):
                sync.validate_channel(dict(channel, **change), self.settings)

    def test_api_redirect_never_forwards_github_token(self):
        url = "https://api.github.com/repos/example/cc-switch/releases/tags/clean-v4.0.35-r2"
        http = sync.Http()
        http.opener = Mock()
        http.opener.open.side_effect = urllib.error.HTTPError(url, 302, "Moved", {"Location": "https://evil.example/steal"}, io.BytesIO())
        with self.assertRaisesRegex(sync.SyncError, "Authenticated redirects"):
            http.open(url, kind="api", token=self.settings.token)
        self.assertEqual(http.opener.open.call_count, 1)
        self.assertEqual(http.opener.open.call_args.args[0].get_header("Authorization"), "Bearer " + self.settings.token)

    def test_github_asset_redirect_uses_no_authorization(self):
        url = "https://github.com/example/cc-switch/releases/download/clean-v4.0.35-r2/file.zip"
        cdn = "https://release-assets.githubusercontent.com/assets/file?download=public"
        http = sync.Http()
        http.opener = Mock()
        http.opener.open.side_effect = [urllib.error.HTTPError(url, 302, "Moved", {"Location": cdn}, io.BytesIO()), Response(cdn)]
        with http.open(url, kind="github"):
            pass
        self.assertEqual(http.opener.open.call_count, 2)
        for call in http.opener.open.call_args_list:
            self.assertIsNone(call.args[0].get_header("Authorization"))

    def test_mirror_refuses_cross_origin_and_http_redirects(self):
        initial = self.settings.base + "/channel.json"
        for destination in ("https://evil.example/cc-switch/channel.json", "http://downloads.example/cc-switch/channel.json",
                            "https://downloads.example/elsewhere/channel.json"):
            http = sync.Http()
            http.opener = Mock()
            http.opener.open.side_effect = urllib.error.HTTPError(initial, 302, "Moved", {"Location": destination}, io.BytesIO())
            with self.subTest(destination=destination), self.assertRaises(sync.SyncError):
                http.open(initial, kind="mirror", base=self.settings.base)
            self.assertEqual(http.opener.open.call_count, 1)

    def test_checksum_parser_rejects_duplicates_paths_and_self_references(self):
        good = f"{'a' * 64}  installer.zip\n".encode()
        self.assertEqual(sync.parse_checksums(good), {"installer.zip": "a" * 64})
        for raw in (good + good, f"{'a' * 64}  ../installer.zip\n".encode(), f"{'a' * 64}  SHA256SUMS.txt\n".encode()):
            with self.assertRaises(sync.SyncError):
                sync.parse_checksums(raw)

    def test_release_gate_rejects_drafts_unexpected_urls_and_extra_files(self):
        files, metadata, _ = self.bundle()
        for kind in ("draft", "url", "extra"):
            value = json.loads(json.dumps(metadata))
            if kind == "draft":
                value["draft"] = True
            elif kind == "url":
                value["assets"][0]["browser_download_url"] = "https://github.com/someone/else/releases/download/tag/file"
            else:
                value["assets"].append({"name": "READY.json", "size": 1, "state": "uploaded",
                                         "browser_download_url": f"https://github.com/{self.settings.repository}/releases/download/{self.settings.tag}/READY.json"})
            with self.subTest(kind=kind), self.assertRaises(sync.SyncError):
                sync.github_release(self.http_fixture(files, value), self.settings)

    def test_full_bundle_hashes_signatures_provenance_and_manifest_are_validated(self):
        files, metadata, manifest = self.bundle()
        http = self.http_fixture(files, metadata)
        with patch.object(sync, "verify_minisign") as verify:
            release = sync.download_release(http, self.settings, {"max_batch_bytes": 10**9}, self.root / "download")
        self.assertEqual(release.manifest, manifest)
        self.assertEqual(set(release.hashes), set(files))
        self.assertEqual(verify.call_count, 10)  # Signed SUMS plus all nine updater artifacts.
        self.assertEqual(http.stream.call_count, len(files))

    def test_invalid_sums_signature_stops_before_downloading_installers(self):
        files, metadata, _ = self.bundle()
        http = self.http_fixture(files, metadata)
        with patch.object(sync, "verify_minisign", side_effect=sync.SyncError("bad signature")):
            with self.assertRaisesRegex(sync.SyncError, "bad signature"):
                sync.download_release(http, self.settings, {"max_batch_bytes": 10**9}, self.root / "download")
        self.assertEqual(http.stream.call_count, 2)

    def test_capacity_gate_stops_before_any_asset_download(self):
        files, metadata, _ = self.bundle()
        http = self.http_fixture(files, metadata)
        with self.assertRaisesRegex(sync.SyncError, "max_batch_bytes"):
            sync.download_release(http, self.settings, {"max_batch_bytes": 1}, self.root / "download")
        http.stream.assert_not_called()

    def test_signed_file_hash_mismatch_is_not_accepted(self):
        files, metadata, _ = self.bundle()
        name = next(name for name in files if name.endswith(".msi"))
        files[name] = b"B" + files[name][1:]
        with patch.object(sync, "verify_minisign"), self.assertRaisesRegex(sync.SyncError, "hash mismatch"):
            sync.download_release(self.http_fixture(files, metadata), self.settings, {"max_batch_bytes": 10**9}, self.root / "download")

    def test_provenance_and_updater_url_are_checked_independently(self):
        files, _, manifest = self.bundle()
        checksums = sync.parse_checksums(files[sync.SUMS])
        provenance = json.loads(files[sync.PROVENANCE])
        for change in ({"repository": "wrong/repo"}, {"signed_updates": False}, {"revision": True}, {"version": "4.0.36"}):
            with self.subTest(change=change), self.assertRaises(sync.SyncError):
                sync.validate_provenance(dict(provenance, **change), self.settings, checksums)
        manifest["platforms"]["darwin-aarch64"]["url"] = "https://github.com/farion1231/cc-switch/releases/download/v4.0.35/official.tar.gz"
        with self.assertRaisesRegex(sync.SyncError, "Updater URL"):
            sync.validate_manifest(manifest, self.settings, {name: SIGNATURE for name in sync.expected_updates("4.0.35").values()})

    def test_sftp_has_strict_pins_and_publishes_ready_only_after_assets(self):
        release = self.release()
        with patch.object(sync.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            sftp = sync.Sftp(self.settings, self.root / "ssh")
            sftp.upload(release, "clean-v4.0.35-r2-123-1-abcd1234")
        self.assertEqual(run.call_count, 3)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["sftp", "-F", "/dev/null"])
        for option in ("-oStrictHostKeyChecking=yes", "-oIdentitiesOnly=yes", "-oBatchMode=yes", "-oPasswordAuthentication=no", "-oIdentityAgent=none"):
            self.assertIn(option, command)
        self.assertNotIn(self.settings.private_key, repr(run.call_args_list))
        self.assertEqual((self.root / "ssh/identity").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("READY.json", run.call_args_list[1].kwargs["input"])
        remote = "/cc-switch/clean-v4.0.35-r2-123-1-abcd1234"
        asset_commands = [shlex.split(line) for line in run.call_args_list[1].kwargs["input"].splitlines()]
        self.assertEqual(asset_commands[0], ["chmod", "2770", remote])
        for index, name in enumerate(sorted(release.sizes)):
            self.assertEqual(asset_commands[1 + index * 2], ["put", str(release.directory / name), remote + "/" + name])
            self.assertEqual(asset_commands[2 + index * 2], ["chmod", "0660", remote + "/" + name])
        self.assertEqual(len(asset_commands), 1 + len(release.sizes) * 2)
        self.assertEqual(run.call_args_list[2].kwargs["input"].count("put "), 1)
        marker_commands = run.call_args_list[2].kwargs["input"].splitlines()
        self.assertTrue(marker_commands[0].endswith('/READY.json.part"'))
        self.assertEqual(shlex.split(marker_commands[1]), ["chmod", "0660", remote + "/READY.json.part"])
        self.assertTrue(marker_commands[2].startswith("rename "))
        self.assertTrue(marker_commands[2].endswith('/READY.json"'))
        self.assertEqual(len(marker_commands), 3)
        ready = json.loads((self.root / "ssh/READY.json").read_text())
        self.assertEqual(ready["files_count"], len(release.sizes))
        self.assertEqual(ready["total_bytes"], sum(release.sizes.values()))

    @unittest.skipUnless(SFTP_SERVER and shutil.which("sftp"), "OpenSSH sftp-server is required for local permission integration")
    def test_actual_local_sftp_preserves_private_sources_and_grants_publisher_group_access(self):
        release = self.release()
        release.directory.chmod(0o700)
        for name in release.sizes:
            (release.directory / name).chmod(0o600)
        inbox = self.root / "staging-inbox"
        inbox.mkdir()
        inbox.chmod(0o2770)
        batch = "clean-v4.0.35-r2-123-1-abcd1234"
        previous_umask = os.umask(0o077)
        try:
            sftp = sync.Sftp(self.settings, self.root / "ssh")
            def local_sftp(batch_text, *, timeout=1800):
                # -D speaks real SFTP over pipes to a local server. Only map
                # the chroot path into this temporary fixture; no SSH, network,
                # production credentials, or remote shell are involved.
                local_batch = batch_text.replace('"/cc-switch/', '"' + str(inbox) + "/")
                return subprocess.run(["sftp", "-D", SFTP_SERVER + " -u 0007", "-b", "-"],
                                      input=local_batch, text=True, capture_output=True, timeout=timeout, check=False)
            with patch.object(sftp, "run", side_effect=local_sftp):
                sftp.upload(release, batch)
        finally:
            os.umask(previous_umask)
        remote = inbox / batch
        self.assertEqual(remote.stat().st_mode & 0o7777, 0o2770)
        self.assertEqual(remote.stat().st_gid, inbox.stat().st_gid)
        self.assertEqual({path.name for path in remote.iterdir()}, set(release.sizes) | {"READY.json"})
        for path in remote.iterdir():
            self.assertEqual(path.stat().st_mode & 0o7777, 0o660, path.name)
            self.assertEqual(path.stat().st_gid, inbox.stat().st_gid, path.name)
        for name in release.sizes:
            self.assertEqual((release.directory / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual((remote / name).read_bytes(), (release.directory / name).read_bytes())
        self.assertEqual((sftp.directory / "READY.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((sftp.directory / "identity").stat().st_mode & 0o777, 0o600)
        self.assertEqual(release.directory.stat().st_mode & 0o777, 0o700)

    def test_interrupted_asset_upload_cleans_only_owned_batch_and_preserves_error(self):
        release = self.release()
        results = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 1, "", "disk quota exceeded"),
                   subprocess.CompletedProcess([], 0, "", "")]
        with patch.object(sync.subprocess, "run", side_effect=results) as run:
            sftp = sync.Sftp(self.settings, self.root / "ssh")
            with self.assertRaisesRegex(sync.SyncError, "disk quota exceeded"):
                sftp.upload(release, "clean-v4.0.35-r2-123-1-abcd1234")
        cleanup = run.call_args_list[-1].kwargs["input"]
        self.assertTrue(all(line.startswith(("-rm ", "-rmdir ")) for line in cleanup.splitlines()))
        self.assertNotIn("READY.json", cleanup)
        self.assertNotIn("/releases/", cleanup)

    def test_unacknowledged_ready_transfer_never_cleans_possibly_published_batch(self):
        release = self.release()
        results = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "", ""),
                   subprocess.CompletedProcess([], 255, "", "connection lost")]
        with patch.object(sync.subprocess, "run", side_effect=results) as run:
            sftp = sync.Sftp(self.settings, self.root / "ssh")
            with self.assertRaisesRegex(sync.SyncError, "batch is preserved"):
                sftp.upload(release, "clean-v4.0.35-r2-123-1-abcd1234")
        self.assertEqual(run.call_count, 3)
        self.assertFalse(any("-rm " in call.kwargs["input"] for call in run.call_args_list))

    def test_receipt_missing_waits_but_host_key_failure_is_fatal(self):
        sftp = sync.Sftp(self.settings, self.root / "ssh")
        batch = "clean-v4.0.35-r2-123-1-abcd1234"
        with patch.object(sftp, "run", return_value=subprocess.CompletedProcess([], 1, "", f'File "/receipts/{batch}.json" not found.')):
            self.assertIsNone(sftp.receipt(batch))
        with patch.object(sftp, "run", return_value=subprocess.CompletedProcess([], 255, "", "Host key verification failed.")):
            with self.assertRaisesRegex(sync.SyncError, "Host key verification failed"):
                sftp.receipt(batch)

    def test_receipts_bind_tag_hash_and_do_not_hide_server_errors(self):
        release = self.release()
        with self.assertRaisesRegex(sync.SyncError, "exact release"):
            sync.validate_receipt(self.receipt(release, checksums_sha256="0" * 64), release)
        with self.assertRaisesRegex(sync.SyncError, "insufficient free space"):
            sync.validate_receipt(self.receipt(release, "error", error="insufficient free space"), release)
        with self.assertRaises(sync.SyncError):
            sync.validate_receipt(self.receipt(release, "superseded", current_tag=self.settings.tag), release)
        sync.validate_receipt(self.receipt(release, "superseded", current_tag="clean-v4.0.36-r2"), release)

    def test_error_before_ready_parse_retains_real_server_diagnosis(self):
        release = self.release()
        receipt = self.receipt(release, "error", release_tag="", checksums_sha256="", message="Permission denied reading READY.json")
        with self.assertRaisesRegex(sync.SyncError, "exact release.*Permission denied"):
            sync.validate_receipt(receipt, release)

    def test_receipt_polling_is_bounded_and_waits_fifteen_seconds(self):
        release = self.release()
        sftp = Mock()
        sftp.receipt.side_effect = [None, self.receipt(release)]
        with patch.object(sync.time, "sleep") as sleep:
            result = sync.wait_receipt(sftp, "safe-batch", release)
        self.assertEqual(result["status"], "published")
        sleep.assert_called_once_with(15)
        sftp.receipt.side_effect = None
        sftp.receipt.return_value = None
        with patch.object(sync.time, "monotonic", side_effect=[0, 0, 0, 600, 600]), patch.object(sync.time, "sleep") as sleep:
            with self.assertRaisesRegex(sync.SyncError, "within 600 seconds"):
                sync.wait_receipt(sftp, "safe-batch", release)
            sleep.assert_not_called()

    def public_single_asset(self, *, corrupt=False, ignore_range=False):
        payload = b"A" * 2048 + b"original ending"
        path = self.root / "installer.msi"
        path.write_bytes(payload)
        release = sync.VerifiedRelease(self.settings, self.root, {path.name: len(payload)},
                                      {path.name: hashlib.sha256(payload).hexdigest()}, {})
        served = payload[:-1] + b"!" if corrupt else payload
        http = sync.Http()
        http.opener = Mock()
        def open_response(request, **kwargs):
            if request.get_method() == "HEAD":
                return Response(request.full_url, headers={"Content-Length": str(len(served))})
            if request.get_header("Range") and not ignore_range:
                return Response(request.full_url, served[:1024], 206, {"Content-Range": f"bytes 0-1023/{len(served)}", "Content-Length": "1024"})
            return Response(request.full_url, served, headers={"Content-Length": str(len(served))})
        http.opener.open.side_effect = open_response
        return http, release

    def test_https_validation_reads_full_asset_after_head_and_real_range(self):
        http, release = self.public_single_asset()
        sync.verify_public_assets(http, release)
        self.assertEqual(http.opener.open.call_count, 3)
        self.assertEqual([call.args[0].get_method() for call in http.opener.open.call_args_list], ["HEAD", "GET", "GET"])

    def test_matching_head_and_range_cannot_hide_corruption_later_in_file(self):
        http, release = self.public_single_asset(corrupt=True)
        with self.assertRaisesRegex(sync.SyncError, "full-download SHA-256 mismatch"):
            sync.verify_public_assets(http, release)
        self.assertEqual(http.opener.open.call_count, 3)

    def test_server_ignoring_range_is_rejected(self):
        http, release = self.public_single_asset(ignore_range=True)
        with self.assertRaisesRegex(sync.SyncError, "does not honor byte ranges"):
            sync.verify_public_assets(http, release)

    def test_stable_manifest_mapping_preserves_every_signature(self):
        release = self.release()
        stable = sync.mapped_manifest(release.manifest, self.settings.base, self.settings.tag)
        http = Mock()
        http.small.return_value = encoded(stable)
        sync.verify_stable(http, release, self.receipt(release), self.root)
        stable["platforms"]["darwin-aarch64"]["signature"] = "modified"
        http.small.return_value = encoded(stable)
        with self.assertRaisesRegex(sync.SyncError, "changes its signatures"):
            sync.verify_stable(http, release, self.receipt(release), self.root)

    def test_superseded_stable_uses_authenticated_newer_metadata(self):
        release = self.release()
        settings = sync.Settings(**{**vars(self.settings), "tag": "clean-v4.0.36-r2"})
        files, _, manifest = self.bundle(settings)
        stable = sync.mapped_manifest(manifest, settings.base, settings.tag)
        http = Mock()
        http.small.side_effect = lambda url, **kwargs: encoded(stable) if "/stable/" in url else files[url.rsplit("/", 1)[1]]
        receipt = self.receipt(release, "superseded", current_tag=settings.tag)
        with patch.object(sync, "verify_minisign") as verify:
            sync.verify_stable(http, release, receipt, self.root)
        verify.assert_called_once()

    def test_healthy_existing_mirror_requires_full_checks_but_never_opens_sftp(self):
        release = self.release()
        with patch.object(sync, "verify_public_assets") as assets, patch.object(sync, "verify_stable") as stable:
            with patch.object(sync, "Sftp") as sftp:
                result = sync.synchronize(Mock(), release, self.root)
        self.assertEqual(result, "already_verified")
        assets.assert_called_once()
        stable.assert_called_once()
        sftp.assert_not_called()

    def test_missing_mirror_uploads_and_requires_complete_final_validation(self):
        release = self.release()
        receipt = self.receipt(release)
        with patch.object(sync, "verify_public_assets", side_effect=[sync.SyncError("HTTP 404"), None]) as assets:
            with patch.object(sync, "verify_stable") as stable, patch.object(sync, "Sftp") as sftp:
                with patch.object(sync, "wait_receipt", return_value=receipt):
                    result = sync.synchronize(Mock(), release, self.root)
        self.assertEqual(result, "published")
        self.assertEqual(assets.call_count, 2)
        sftp.return_value.upload.assert_called_once()
        stable.assert_called_once()

    def test_failed_final_full_hash_validation_remains_a_failure(self):
        release = self.release()
        with patch.object(sync, "verify_public_assets", side_effect=[sync.SyncError("HTTP 404"), sync.SyncError("full hash mismatch")]):
            with patch.object(sync, "Sftp"), patch.object(sync, "wait_receipt", return_value=self.receipt(release)):
                with self.assertRaisesRegex(sync.SyncError, "full hash mismatch"):
                    sync.synchronize(Mock(), release, self.root)

    def test_http_identity_violation_never_becomes_a_repair_upload(self):
        release = self.release()
        with patch.object(sync, "verify_public_assets", side_effect=sync.HttpBoundaryError("cross-origin redirect")):
            with patch.object(sync, "Sftp") as sftp:
                with self.assertRaisesRegex(sync.SyncError, "cross-origin redirect"):
                    sync.synchronize(Mock(), release, self.root)
        sftp.assert_not_called()

    @unittest.skipUnless(shutil.which("minisign"), "minisign CLI is required for the real signature fixture")
    def test_actual_minisign_fixture_accepts_original_and_rejects_tampered_payload(self):
        path = self.root / "signed-fixture.txt"
        path.write_bytes(SIGNED_PAYLOAD)
        sync.verify_minisign(path, SIGNATURE.encode(), PUBLIC_KEY)
        path.write_bytes(SIGNED_PAYLOAD + b"tampered")
        with self.assertRaisesRegex(sync.SyncError, "Signature verification failed"):
            sync.verify_minisign(path, SIGNATURE.encode(), PUBLIC_KEY)

    @unittest.skipUnless(shutil.which("minisign"), "minisign CLI is required for the real signature fixture")
    def test_actual_signature_rejects_a_different_public_key(self):
        path = self.root / "signed-fixture.txt"
        path.write_bytes(SIGNED_PAYLOAD)
        key = bytearray(sync.key_material(PUBLIC_KEY))
        key[-1] ^= 1
        other = "untrusted comment: altered fixture key\n" + base64.b64encode(key).decode() + "\n"
        with self.assertRaisesRegex(sync.SyncError, "Signature verification failed"):
            sync.verify_minisign(path, SIGNATURE.encode(), other)


@unittest.skipUnless(shutil.which("minisign"), "minisign CLI is required for the local protocol integration")
class ProtocolIntegration(unittest.TestCase):
    """Real crypto and publisher filesystem; HTTP and SFTP are local stubs.

    This verifies the shared wire/data protocol, not production TLS, SSH,
    production permissions, bandwidth, DNS, systemd or the Caddy deployment.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = io.StringIO()
        capture = contextlib.redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.private = self.root / "fixture-secret.key"
        self.public = self.root / "fixture-public.key"
        subprocess.run(["minisign", "-G", "-W", "-s", str(self.private), "-p", str(self.public)],
                       capture_output=True, check=True)
        key = base64.b64encode(self.public.read_bytes()).decode()
        self.settings = sync.Settings("example/cc-switch", "clean-v4.0.35-r2", "unused-test-token", key,
                                      "https://downloads.example.com/cc-switch", "upload.example", 22, "ccswitch_upload",
                                      "SFTP IS MOCKED IN THIS TEST", f"upload.example ssh-ed25519 {HOST_KEY}\n")
        server_directory = Path(__file__).resolve().parents[1] / "server"
        protocol_spec = importlib.util.spec_from_file_location("sync_contract_protocol", server_directory / "_protocol.py")
        protocol = importlib.util.module_from_spec(protocol_spec)
        protocol_spec.loader.exec_module(protocol)
        publisher_spec = importlib.util.spec_from_file_location("sync_contract_publisher", server_directory / "publisher.py")
        publisher_module = importlib.util.module_from_spec(publisher_spec)
        # publisher.py uses a deployment-local _protocol import. Isolate this
        # test's binding and restore sys.path so other suites cannot collide.
        previous_path = sys.path[:]
        try:
            with patch.dict(sys.modules, {"_protocol": protocol, "sync_contract_publisher": publisher_module}):
                publisher_spec.loader.exec_module(publisher_module)
        finally:
            sys.path[:] = previous_path
        server_root = self.root / "server"
        for name in ("incoming/cc-switch", "incoming/receipts", "work/cc-switch", "public/cc-switch"):
            (server_root / name).mkdir(parents=True)
        (server_root / "work/cc-switch").chmod(0o700)
        self.publisher = publisher_module.Publisher({"root": str(server_root), "repository": self.settings.repository,
                                                     "base_url": self.settings.base, "public_key_file": str(self.public),
                                                     "max_batch_bytes": 2**20, "min_free_bytes": 1})
        self.publisher.write_channel()
        self.http = sync.Http()
        self.http.opener = Mock(side_effect=None)
        self.http.opener.open.side_effect = self.public_response
        self.sequence = 0

    def sign(self, path):
        raw = self.root / "current-signature.minisig"
        raw.unlink(missing_ok=True)
        subprocess.run(["minisign", "-S", "-W", "-s", str(self.private), "-m", str(path), "-x", str(raw),
                        "-t", "CC Switch local protocol fixture"], capture_output=True, check=True)
        return base64.b64encode(raw.read_bytes()) + b"\n"

    def make_release(self, tag):
        settings = sync.Settings(**{**vars(self.settings), "tag": tag})
        version, revision, _ = sync.parse_tag(tag)
        source = self.root / (tag + "-source")
        source.mkdir()
        for name in sync.expected_assets(version) - {sync.SUMS, sync.SUMS_SIG, sync.PROVENANCE, "latest.json"}:
            if not name.endswith(".sig"):
                (source / name).write_bytes(name.encode() + b"\n" + b"fixture artifact bytes\n" * 100)
        signatures = {}
        for name in sorted(set(sync.expected_updates(version).values())):
            signature = self.sign(source / name)
            (source / (name + ".sig")).write_bytes(signature)
            signatures[name] = signature.decode().strip()
        manifest = {"version": version, "notes": "Local protocol fixture", "pub_date": "2026-11-20T00:00:00Z", "platforms": {
            platform: {"url": f"https://github.com/{settings.repository}/releases/download/{tag}/{name}", "signature": signatures[name]}
            for platform, name in sync.expected_updates(version).items()}}
        (source / "latest.json").write_bytes(encoded(manifest))
        provenance = {"schema": 1, "repository": settings.repository, "release_tag": tag, "version": version,
                      "revision": revision, "signed_updates": True, "updater_public_key": settings.public_key,
                      "asset_sha256": {path.name: sync.sha256(path) for path in source.iterdir()}}
        (source / sync.PROVENANCE).write_bytes(encoded(provenance))
        checksums = {path.name: sync.sha256(path) for path in source.iterdir()}
        (source / sync.SUMS).write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())))
        (source / sync.SUMS_SIG).write_bytes(self.sign(source / sync.SUMS))
        files = {path.name: path.read_bytes() for path in source.iterdir()}
        metadata = {"tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-11-20T00:00:00Z", "assets": [
            {"name": name, "size": len(data), "state": "uploaded",
             "browser_download_url": f"https://github.com/{settings.repository}/releases/download/{tag}/{name}"}
            for name, data in files.items()]}
        github = Mock()
        github.small.return_value = encoded(metadata)
        def transfer(url, size, *, destination, **kwargs):
            data = files[url.rsplit("/", 1)[1]]
            self.assertEqual(size, len(data))
            destination.write_bytes(data)
            return hashlib.sha256(data).hexdigest()
        github.stream.side_effect = transfer
        # No crypto mocks: the client verifies the signed SUM and all updater
        # artifacts before this fixture can reach the real publisher.
        channel = self.publisher.channel()
        sync.validate_channel(channel, settings)
        return sync.download_release(github, settings, channel, self.root / (tag + "-client"))

    def public_response(self, request, **kwargs):
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, "downloads.example.com")
        self.assertTrue(url.path.startswith("/cc-switch/"))
        path = self.publisher.public / url.path[len("/cc-switch/"):]
        if not path.is_file():
            raise urllib.error.HTTPError(request.full_url, 404, "Missing", {}, io.BytesIO())
        data = path.read_bytes()
        if request.get_method() == "HEAD":
            return Response(request.full_url, headers={"Content-Length": str(len(data))})
        byte_range = request.get_header("Range")
        if byte_range:
            self.assertTrue(byte_range.startswith("bytes=0-"))
            last = int(byte_range.split("-", 1)[1])
            return Response(request.full_url, data[:last + 1], 206,
                            {"Content-Length": str(last + 1), "Content-Range": f"bytes 0-{last}/{len(data)}"})
        return Response(request.full_url, data, headers={"Content-Length": str(len(data))})

    def publish(self, release):
        self.sequence += 1
        batch = f"{release.settings.tag}-123-1-{self.sequence:08x}"
        sftp = sync.Sftp(release.settings, self.root / f"ssh-{self.sequence}")
        def remote_path(value):
            self.assertTrue(value.startswith("/cc-switch/"))
            self.assertNotIn("..", value.split("/"))
            return self.publisher.inbox / value[len("/cc-switch/"):]
        def execute(batch_text, **kwargs):
            for line in batch_text.splitlines():
                command, *arguments = shlex.split(line)
                if command == "mkdir":
                    remote_path(arguments[0]).mkdir()
                elif command == "put":
                    shutil.copyfile(arguments[0], remote_path(arguments[1]))
                    remote_path(arguments[1]).chmod(Path(arguments[0]).stat().st_mode & 0o777)
                    if arguments[1].endswith("/READY.json.part"):
                        self.assertEqual(self.publisher.once(), [], "A partial marker must not become visible to the scanner")
                elif command == "chmod":
                    remote_path(arguments[1]).chmod(int(arguments[0], 8))
                elif command == "rename":
                    self.assertEqual(remote_path(arguments[0]).stat().st_mode & 0o777, 0o660)
                    os.rename(remote_path(arguments[0]), remote_path(arguments[1]))
                else:
                    self.fail(f"Unexpected command in local SFTP protocol stub: {command}")
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(sftp, "run", side_effect=execute):
            sftp.upload(release, batch)
        self.assertEqual((self.publisher.inbox / batch).stat().st_mode & 0o7777, 0o2770)
        for path in (self.publisher.inbox / batch).iterdir():
            self.assertEqual(path.stat().st_mode & 0o7777, 0o660, path.name)
        receipt = self.publisher.once()[0]
        sync.validate_receipt(receipt, release)
        self.assertFalse((self.publisher.inbox / batch).exists())
        self.assertEqual(json.loads((self.publisher.receipts / (batch + ".json")).read_text()), receipt)
        self.assertEqual((self.publisher.releases / release.settings.tag / "latest.json").read_bytes(),
                         (release.directory / "latest.json").read_bytes())
        sync.verify_public_assets(self.http, release)
        sync.verify_stable(self.http, release, receipt, self.root)
        return receipt

    def test_real_client_publisher_and_public_download_contract(self):
        release = self.make_release("clean-v4.0.35-r2")
        self.assertEqual(self.publish(release)["status"], "published")
        self.assertEqual(self.publish(release)["status"], "already_published")
        newer = self.make_release("clean-v4.0.36-r2")
        self.assertEqual(self.publish(newer)["status"], "published")
        superseded = self.publish(release)
        self.assertEqual(superseded["status"], "superseded")
        self.assertEqual(superseded["current_tag"], newer.settings.tag)
        # The public stable link remains newer, while the old immutable files
        # and all their original signed checksum bytes remain downloadable.
        self.assertEqual(self.publisher.current_tag(), newer.settings.tag)


if __name__ == "__main__":
    unittest.main()
