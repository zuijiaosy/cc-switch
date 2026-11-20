"""Real minisign tests for the restricted-SFTP publisher; no network or server use."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("download_server", Path(__file__).resolve().parents[1] / "server/publisher.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class DownloadPublisher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("minisign"):
            raise RuntimeError("Real publisher tests require minisign; install minisign before running the Python suite")
        cls.fixture_temp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_temp.name)
        cls.secret = cls.fixture / "test-secret.key"
        cls.public = cls.fixture / "test-public.key"
        subprocess.run(["minisign", "-G", "-W", "-s", str(cls.secret), "-p", str(cls.public)],
                       check=True, capture_output=True)
        cls.key = base64.b64encode(cls.public.read_bytes()).decode("ascii")
        cls.bundles = {}

    @classmethod
    def tearDownClass(cls):
        cls.fixture_temp.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.output = io.StringIO()
        capture = contextlib.redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        for relative in ("incoming/cc-switch", "incoming/receipts", "work/cc-switch", "public/cc-switch"):
            (self.root / relative).mkdir(parents=True)
        os.chmod(self.root / "work/cc-switch", 0o700)
        self.key_file = self.root / "updater.pub"
        self.key_file.write_bytes(self.public.read_bytes())
        self.configuration = {"root": str(self.root), "repository": "example/cc-switch", "base_url": "https://downloads.example.com/cc-switch",
                              "public_key_file": str(self.key_file), "max_batch_bytes": 2 * 1024**3, "min_free_bytes": 1024}
        self.publisher = server.Publisher(self.configuration)
        self.publisher.write_channel()
        self.sequence = 0

    @classmethod
    def sign(cls, raw):
        path = cls.fixture / "payload.bin"
        signature = cls.fixture / "payload.minisig"
        path.write_bytes(raw)
        subprocess.run(["minisign", "-S", "-s", str(cls.secret), "-m", str(path), "-x", str(signature)],
                       check=True, capture_output=True)
        return base64.b64encode(signature.read_bytes()) + b"\n"

    def seal(self, files):
        files = dict(files)
        for name in (server.SUMS, server.SUMS_SIG):
            files.pop(name, None)
        provenance = json.loads(files[server.PROVENANCE])
        provenance["asset_sha256"] = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items() if name != server.PROVENANCE}
        files[server.PROVENANCE] = server.encoded(provenance)
        files[server.SUMS] = "".join(f"{hashlib.sha256(raw).hexdigest()}  {name}\n" for name, raw in sorted(files.items())).encode()
        files[server.SUMS_SIG] = self.sign(files[server.SUMS])
        return files

    def bundle(self, tag="clean-v4.0.35-r2"):
        if tag in self.bundles:
            return dict(self.bundles[tag])
        version, revision, _ = server.parse_tag(tag)
        names = server.expected_assets(version) - {server.PROVENANCE, server.SUMS, server.SUMS_SIG, "latest.json"}
        files = {name: (b"Test installation payload: " + name.encode() + b"\n" + b"A" * 1024) for name in names if not name.endswith(".sig")}
        for name in sorted(set(server.expected_updates(version).values())):
            files[name + ".sig"] = self.sign(files[name])
        files["latest.json"] = server.encoded({"version": version, "notes": "Test release", "pub_date": "2026-11-20T00:00:00Z", "platforms": {
            platform: {"url": f"https://github.com/example/cc-switch/releases/download/{tag}/{name}",
                       "signature": files[name + ".sig"].decode("ascii").strip()}
            for platform, name in server.expected_updates(version).items()}})
        files[server.PROVENANCE] = server.encoded({"schema": 1, "repository": "example/cc-switch", "release_tag": tag,
                                                  "version": version, "revision": revision, "signed_updates": True,
                                                  "updater_public_key": self.key, "asset_sha256": {}})
        files = self.seal(files)
        self.bundles[tag] = dict(files)
        return files

    def batch(self, tag="clean-v4.0.35-r2", files=None, ready_change=None):
        files = self.bundle(tag) if files is None else files
        self.sequence += 1
        name = f"{tag}-123-{self.sequence}-{self.sequence:08x}"
        directory = self.publisher.inbox / name
        directory.mkdir()
        for filename, raw in files.items():
            (directory / filename).write_bytes(raw)
        ready = {"schema": 1, "repository": "example/cc-switch", "release_tag": tag,
                 "checksums_sha256": hashlib.sha256(files[server.SUMS]).hexdigest(),
                 "files_count": len(files), "total_bytes": sum(len(raw) for raw in files.values())}
        ready.update(ready_change or {})
        (directory / "READY.json").write_bytes(server.encoded(ready))
        return name, directory, files

    def run_batch(self, *args, **kwargs):
        name, directory, files = self.batch(*args, **kwargs)
        results = self.publisher.once()
        self.assertEqual(len(results), 1)
        self.assertFalse(directory.exists(), "Terminal batches must release inbox quota")
        self.assertEqual(list(self.publisher.work.glob(".publish-*")), [], "Private snapshots must be cleaned")
        receipt = json.loads((self.publisher.receipts / (name + ".json")).read_text())
        self.assertEqual(results[0], receipt)
        return receipt, files

    def test_bootstrap_writes_only_identity_and_entry_page(self):
        channel = json.loads((self.publisher.public / "channel.json").read_text())
        self.assertEqual(channel["schema"], 1)
        self.assertEqual(channel["application"], "cc-switch")
        self.assertEqual(server.key_material(channel["updater_public_key"]), server.key_material(self.key))
        self.assertFalse((self.publisher.public / "stable").exists())
        self.assertEqual(list(self.publisher.releases.iterdir()), [])

    def test_real_signed_full_release_publishes_originals_and_derived_channel(self):
        receipt, files = self.run_batch()
        self.assertEqual(receipt["status"], "published")
        tag = receipt["release_tag"]
        destination = self.publisher.releases / tag
        for name, raw in files.items():
            self.assertEqual((destination / name).read_bytes(), raw)
            self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((destination / "channel").stat().st_mode), 0o755)
        self.assertEqual(os.readlink(self.publisher.public / "stable"), f"releases/{tag}/channel")
        original = json.loads(files["latest.json"])
        latest = json.loads((self.publisher.public / "stable/latest.json").read_text())
        for platform, name in server.expected_updates("4.0.35").items():
            self.assertEqual(latest["platforms"][platform]["url"], f"{self.publisher.base}/releases/{tag}/{name}")
            self.assertEqual(latest["platforms"][platform]["signature"], original["platforms"][platform]["signature"])
        self.assertEqual(stat.S_IMODE(next(self.publisher.receipts.glob("*.json")).stat().st_mode), 0o640)

    def test_identical_retry_is_idempotent_and_retains_inode(self):
        first, _ = self.run_batch()
        directory = self.publisher.releases / first["release_tag"]
        inode = directory.stat().st_ino
        retry, _ = self.run_batch()
        self.assertEqual(retry["status"], "already_published")
        self.assertEqual(directory.stat().st_ino, inode)

    def test_public_permissions_do_not_inherit_restrictive_upload_umask(self):
        original = os.umask(0o007)
        try:
            receipt, _ = self.run_batch()
        finally:
            os.umask(original)
        directory = self.publisher.releases / receipt["release_tag"]
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((directory / "channel").stat().st_mode), 0o755)
        for path in directory.rglob("*"):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_download_page_groups_installers_before_signature_metadata(self):
        receipt, _ = self.run_batch()
        page = (self.publisher.public / "stable/index.html").read_text()
        for title in ("macOS", "Windows x64", "Windows ARM64", "Linux x64", "Linux ARM64", "校验与构建来源"):
            self.assertIn(f"<h2>{title}</h2>", page)
        for name in ("macOS.dmg", "Windows.msi", "Windows-arm64.msi", "Linux-x86_64.AppImage", "Linux-arm64.AppImage"):
            self.assertIn(f"/releases/{receipt['release_tag']}/CC-Switch-v4.0.35-{name}", page)
        self.assertLess(page.index("校验与构建来源"), page.index("SHA256SUMS.txt"))
        for text in ("CodexZH", "CcodeZH", "衍生构建", "并非 CC Switch 官方发布", "https://github.com/example/cc-switch",
                     "https://github.com/farion1231/cc-switch/releases/tag/v4.0.35", "完整构建源码"):
            self.assertIn(text, page)

    def test_same_tag_different_signed_content_never_overwrites_public_files(self):
        first, files = self.run_batch()
        name = "CC-Switch-v4.0.35-Windows-Portable.zip"
        changed = dict(files)
        changed[name] = b"Different but authorized checksum content"
        receipt, _ = self.run_batch(files=self.seal(changed))
        self.assertEqual(receipt["status"], "error")
        self.assertIn("different signed checksums", receipt["message"])
        self.assertEqual((self.publisher.releases / first["release_tag"] / name).read_bytes(), files[name])

    def test_older_release_is_available_but_cannot_roll_back_stable(self):
        newer, _ = self.run_batch(tag="clean-v4.0.36-r2")
        older, _ = self.run_batch()
        self.assertEqual(older["status"], "superseded")
        self.assertEqual(older["current_tag"], newer["release_tag"])
        self.assertTrue((self.publisher.releases / older["release_tag"]).is_dir())
        retry, _ = self.run_batch()
        self.assertEqual(retry["status"], "superseded")
        self.assertEqual(os.readlink(self.publisher.public / "stable"), f"releases/{newer['release_tag']}/channel")

    def test_same_app_version_revision_is_compared_numerically(self):
        self.run_batch(tag="clean-v4.0.35-r10")
        receipt, _ = self.run_batch(tag="clean-v4.0.35-r2")
        self.assertEqual(receipt["status"], "superseded")
        self.assertEqual(receipt["current_tag"], "clean-v4.0.35-r10")

    def test_signed_checksum_tampering_is_rejected_before_payload_copy(self):
        files = self.bundle()
        files[server.SUMS] = files[server.SUMS].replace(b"  CC-Switch", b"  DC-Switch", 1)
        with patch.object(self.publisher, "copy_payload", wraps=self.publisher.copy_payload) as copy:
            receipt, _ = self.run_batch(files=files)
            copy.assert_not_called()
        self.assertEqual(receipt["status"], "error")
        self.assertIn("Signature verification failed", receipt["message"])
        self.assertFalse((self.publisher.public / "stable").exists())

    def test_signed_checksum_does_not_replace_individual_updater_signature_validation(self):
        files = self.bundle()
        files["CC-Switch-v4.0.35-Windows.msi"] += b"tampered"
        receipt, _ = self.run_batch(files=self.seal(files))
        self.assertEqual(receipt["status"], "error")
        self.assertIn("Signature verification failed", receipt["message"])

    def test_unsigned_payload_tampering_rejected_and_cleaned(self):
        files = self.bundle()
        files["CC-Switch-v4.0.35-Windows-Portable.zip"] += b"tampered"
        receipt, _ = self.run_batch(files=files)
        self.assertEqual(receipt["status"], "error")
        self.assertIn("hash mismatch", receipt["message"])

    def test_duplicate_or_traversal_checksum_entries_are_rejected(self):
        for suffix in (b"../escape", b"duplicate"):
            with self.subTest(suffix=suffix):
                files = self.bundle()
                addition = files[server.SUMS].splitlines()[0] if suffix == b"duplicate" else b"a" * 64 + b"  ../escape"
                files[server.SUMS] += addition + b"\n"
                files[server.SUMS_SIG] = self.sign(files[server.SUMS])
                receipt, _ = self.run_batch(files=files)
                self.assertEqual(receipt["status"], "error")

    def test_missing_platform_and_extra_files_are_rejected(self):
        for change in ("missing", "extra"):
            with self.subTest(change=change):
                files = self.bundle()
                if change == "missing":
                    del files["CC-Switch-v4.0.35-Linux-arm64.rpm"]
                else:
                    files["unexpected.txt"] = b"extra"
                receipt, _ = self.run_batch(files=files)
                self.assertEqual(receipt["status"], "error")
                self.assertIn("missing, extra", receipt["message"])

    def test_symlink_payload_is_rejected_without_touching_target(self):
        name, directory, files = self.batch()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"Do not read or modify")
        payload = directory / "CC-Switch-v4.0.35-Windows-Portable.zip"
        payload.unlink()
        payload.symlink_to(outside)
        receipt = self.publisher.once()[0]
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(outside.read_bytes(), b"Do not read or modify")
        self.assertFalse(directory.exists())

    def test_copy_to_private_work_prevents_later_inbox_mutation(self):
        name, directory, files = self.batch()
        original = self.publisher.validate_snapshot
        target = "CC-Switch-v4.0.35-Windows-Portable.zip"
        def mutate_then_verify(snapshot, tag, checksums):
            (directory / target).write_bytes(b"changed after private copy")
            return original(snapshot, tag, checksums)
        with patch.object(self.publisher, "validate_snapshot", side_effect=mutate_then_verify):
            receipt = self.publisher.once()[0]
        self.assertEqual(receipt["status"], "published")
        self.assertEqual((self.publisher.releases / receipt["release_tag"] / target).read_bytes(), files[target])

    def test_provenance_repository_revision_key_and_unsigned_flag_are_rejected(self):
        changes = ({"repository": "other/repo"}, {"revision": True}, {"signed_updates": False}, {"updater_public_key": "invalid"})
        for change in changes:
            with self.subTest(change=change):
                files = self.bundle()
                provenance = json.loads(files[server.PROVENANCE])
                provenance.update(change)
                files[server.PROVENANCE] = server.encoded(provenance)
                receipt, _ = self.run_batch(files=self.seal(files))
                self.assertEqual(receipt["status"], "error")

    def test_manifest_wrong_host_or_platform_set_is_rejected_even_when_signed(self):
        for change in ("url", "platform"):
            with self.subTest(change=change):
                files = self.bundle()
                manifest = json.loads(files["latest.json"])
                if change == "url":
                    manifest["platforms"]["windows-x86_64"]["url"] = "https://evil.example/old.msi"
                else:
                    del manifest["platforms"]["windows-aarch64"]
                files["latest.json"] = server.encoded(manifest)
                receipt, _ = self.run_batch(files=self.seal(files))
                self.assertEqual(receipt["status"], "error")

    def test_disk_budget_failure_preserves_stable_and_releases_inbox_quota(self):
        first, _ = self.run_batch()
        usage = shutil._ntuple_diskusage(100000, 99000, 1000)
        with patch.object(server.shutil, "disk_usage", return_value=usage):
            receipt, _ = self.run_batch(tag="clean-v4.0.36-r2")
        self.assertEqual(receipt["status"], "error")
        self.assertIn("Insufficient free disk space", receipt["message"])
        self.assertEqual(os.readlink(self.publisher.public / "stable"), f"releases/{first['release_tag']}/channel")

    def test_ready_byte_limit_and_count_are_enforced(self):
        for change in ({"total_bytes": self.publisher.max_batch + 1}, {"files_count": True}, {"total_bytes": 1}):
            with self.subTest(change=change):
                receipt, _ = self.run_batch(ready_change=change)
                self.assertEqual(receipt["status"], "error")

    def test_failed_atomic_switch_keeps_old_stable_and_retry_uses_immutable_files(self):
        first, _ = self.run_batch()
        real_replace = server.os.replace
        def fail_stable(source, destination, *args, **kwargs):
            if Path(destination) == self.publisher.public / "stable":
                raise OSError("Simulated atomic switch failure")
            return real_replace(source, destination, *args, **kwargs)
        with patch.object(server.os, "replace", side_effect=fail_stable):
            failed, _ = self.run_batch(tag="clean-v4.0.36-r2")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(os.readlink(self.publisher.public / "stable"), f"releases/{first['release_tag']}/channel")
        immutable = self.publisher.releases / "clean-v4.0.36-r2"
        inode = immutable.stat().st_ino
        retried, _ = self.run_batch(tag="clean-v4.0.36-r2")
        self.assertEqual(retried["status"], "already_published")
        self.assertEqual(immutable.stat().st_ino, inode)
        self.assertEqual(os.readlink(self.publisher.public / "stable"), "releases/clean-v4.0.36-r2/channel")

    def test_only_abandoned_protocol_batches_expire(self):
        _, abandoned, _ = self.batch()
        (abandoned / "READY.json").unlink()
        old = time.time() - server.EXPIRE_SECONDS - 10
        for path in abandoned.iterdir():
            os.utime(path, (old, old))
        os.utime(abandoned, (old, old))
        _, active, _ = self.batch()
        (active / "READY.json").unlink()
        os.utime(active, (old, old))
        unrelated = self.publisher.inbox / "unrelated-old-directory"
        unrelated.mkdir()
        os.utime(unrelated, (old, old))
        self.assertEqual(self.publisher.once(), [])
        self.assertFalse(abandoned.exists())
        self.assertTrue(active.exists())
        self.assertTrue(unrelated.exists())

    def test_unsafe_download_domains_are_rejected(self):
        for value in ("http://downloads.example.com/cc-switch", "https://localhost/cc-switch", "https://127.0.0.1/cc-switch",
                      "https://downloads.example.com:8443/cc-switch", "https://a:b@downloads.example.com/cc-switch",
                      "https://downloads.example.com/cc-switch?secret=x", "https://downloads.example.com/other",
                      "https://downloads.local/cc-switch", "https://0x7f.1/cc-switch", "https://0177.1/cc-switch",
                      "https://downloads.example.com/cc-switch////"):
            with self.subTest(value=value), self.assertRaises(server.PublishError):
                server.normalize_base(value)
        self.assertEqual(server.normalize_base("https://DOWNLOADS.example.com:443/cc-switch/"), "https://downloads.example.com/cc-switch")

    def test_group_writable_public_or_receipt_directories_fail_without_auto_chmod(self):
        for path in (self.publisher.public, self.publisher.releases, self.publisher.receipts, self.root / "incoming", self.root / "public"):
            with self.subTest(path=str(path)):
                old = stat.S_IMODE(path.stat().st_mode)
                os.chmod(path, old | 0o020)
                try:
                    with self.assertRaisesRegex(server.PublishError, "Unsafe owner or group/world-write"):
                        self.publisher.check_layout()
                    self.assertTrue(path.stat().st_mode & 0o020, "Unsafe permissions must be reported, not silently repaired")
                finally:
                    os.chmod(path, old)

    def test_group_writable_trusted_public_key_is_rejected(self):
        os.chmod(self.key_file, 0o664)
        with self.assertRaisesRegex(server.PublishError, "Unsafe owner or group/world-write"):
            server.Publisher(self.configuration)


if __name__ == "__main__":
    unittest.main()
