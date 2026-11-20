"""Offline tests for the MinIO download mirror (no AWS CLI, network, or gh)."""

import contextlib
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sync_minio", Path(__file__).resolve().parents[1] / "scripts/sync-minio.py")
mirror = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mirror
SPEC.loader.exec_module(mirror)

TAG = "clean-v4.0.35-r1"
INSTALLERS = [f"CC-Switch-v4.0.35-{suffix}" for suffix, *_ in mirror.KINDS]
UPDATER_ONLY = ["latest.json", "CC-Switch-v4.0.35-macOS.tar.gz", "CC-Switch-v4.0.35-Windows.msi.sig"]
ENV = {
    "GITHUB_REPOSITORY": "zuijiaosy/cc-switch",
    "MINIO_ENDPOINT": "https://oss.example.com",
    "MINIO_BUCKET": "downloads",
    "MINIO_ACCESS_KEY_ID": "test-access",
    "MINIO_SECRET_ACCESS_KEY": "test-secret",
}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def head(self, key):
        item = self.objects.get(key)
        return None if item is None else {"ContentLength": len(item), "Metadata": {"sha256": hashlib.sha256(item).hexdigest()}}

    def get(self, key, destination):
        if key not in self.objects:
            return False
        Path(destination).write_bytes(self.objects[key])
        return True

    def put(self, path, key, *, cache_control, digest, attachment):
        body = Path(path).read_bytes()
        assert hashlib.sha256(body).hexdigest() == digest
        self.objects[key] = body
        self.puts.append((key, cache_control, attachment))


def release_files(tag=TAG, signed=False):
    version = mirror.parse_tag(tag)[0]
    files = {name.replace("v4.0.35", f"v{version}"): f"{name} payload\n".encode() for name in INSTALLERS}
    files.update({name.replace("v4.0.35", f"v{version}"): b"updater\n" for name in UPDATER_ONLY})
    files[mirror.PROVENANCE] = json.dumps({"release_tag": tag, "repository": ENV["GITHUB_REPOSITORY"],
                                           "upstream_repository": "farion1231/cc-switch", "macos_notarized": False}).encode()
    files[mirror.SUMS] = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in sorted(files.items())).encode()
    if signed:
        files[mirror.SUMS_SIG] = b"signature\n"
    return files


def release_json(files, tag=TAG):
    return {"tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-11-19T03:25:04Z",
            "html_url": f"https://github.com/zuijiaosy/cc-switch/releases/tag/{tag}",
            "assets": [{"name": name, "size": len(body)} for name, body in files.items()]}


class Harness:
    def __init__(self, files, tag=TAG):
        self.files, self.tag, self.public = files, tag, {}

    def download(self, repository, tag, names, directory):
        for name in names:
            (Path(directory) / name).write_bytes(self.files[name])

    def public_digest(self, url):
        body = self.public[url]
        return len(body), hashlib.sha256(body).hexdigest()

    def http_get(self, url, headers=None):
        body = self.public[url]
        if headers and "Range" in headers:
            return 206, {}, body[:1024].ljust(1024, b"\0")
        return 200, {}, body


class SyncMinioTests(unittest.TestCase):
    def setUp(self):
        self.settings = mirror.settings_from_env(ENV)

    def run_sync(self, s3, files, tag=TAG):
        harness = Harness(files, tag)

        def mirror_public():
            for key, body in s3.objects.items():
                harness.public[f"{self.settings.public_base}/{key}"] = body

        def public_digest(url):
            mirror_public()
            return harness.public_digest(url)

        def http_get(url, headers=None, attempts=5):
            mirror_public()
            return harness.http_get(url, headers)

        with patch.object(mirror, "resolve_release", return_value=release_json(files, tag)), \
                patch.object(mirror, "download_assets", harness.download), \
                patch.object(mirror, "public_digest", public_digest), \
                patch.object(mirror, "http_get", http_get), \
                patch.object(mirror, "verify_minisign") as verify, \
                contextlib.redirect_stdout(io.StringIO()):
            mirror.synchronize(self.settings, s3, tag)
        return verify

    def test_settings_validation(self):
        self.assertEqual(self.settings.root, "https://oss.example.com/downloads/cc-switch")
        for key, value in (("MINIO_ENDPOINT", "http://oss.example.com"), ("MINIO_ENDPOINT", "https://oss.example.com/x"),
                           ("MINIO_BUCKET", "Bad_Bucket"), ("MINIO_PREFIX", "a/b"), ("MINIO_SECRET_ACCESS_KEY", "")):
            with self.subTest(key=key), self.assertRaises(mirror.SyncError):
                mirror.settings_from_env({**ENV, key: value})
        custom = mirror.settings_from_env({**ENV, "MINIO_PUBLIC_BASE_URL": "https://dl.example.com/files/"})
        self.assertEqual(custom.public_url("latest.json"), "https://dl.example.com/files/cc-switch/latest.json")

    def test_selection_skips_updater_assets_and_requires_every_platform(self):
        names = mirror.mirrored_names(set(release_files()), "v4.0.35")
        self.assertEqual(len(names), len(INSTALLERS) + 2)
        self.assertFalse(set(UPDATER_ONLY) & set(names))
        incomplete = set(release_files()) - {"CC-Switch-v4.0.35-Linux-arm64.rpm"}
        with self.assertRaises(mirror.SyncError):
            mirror.mirrored_names(incomplete, "v4.0.35")

    def test_first_mirror_uploads_verifies_and_publishes_manifest(self):
        s3, files = FakeS3(), release_files()
        verify = self.run_sync(s3, files)
        verify.assert_not_called()
        manifest = json.loads(s3.objects["cc-switch/latest.json"])
        self.assertEqual(manifest["release_tag"], TAG)
        self.assertFalse(manifest["signed"])
        self.assertEqual({(f["os"], f["arch"], f["kind"]) for f in manifest["files"]}, mirror.REQUIRED_KINDS)
        dmg = next(f for f in manifest["files"] if f["kind"] == "dmg")
        self.assertEqual(dmg["url"], f"https://oss.example.com/downloads/cc-switch/releases/{TAG}/CC-Switch-v4.0.35-macOS.dmg")
        self.assertEqual(dmg["sha256"], hashlib.sha256(files["CC-Switch-v4.0.35-macOS.dmg"]).hexdigest())
        caches = dict((key, cache) for key, cache, _ in s3.puts)
        self.assertEqual(caches["cc-switch/latest.json"], mirror.MUTABLE)
        self.assertEqual(caches[f"cc-switch/releases/{TAG}/CC-Switch-v4.0.35-macOS.dmg"], mirror.IMMUTABLE)
        self.assertNotIn(f"cc-switch/releases/{TAG}/latest.json", s3.objects)

    def test_second_run_is_a_no_op(self):
        s3, files = FakeS3(), release_files()
        self.run_sync(s3, files)
        count = len(s3.puts)
        with patch.object(mirror, "download_assets", side_effect=AssertionError("should not download")):
            self.run_sync(s3, files)
        self.assertEqual(len(s3.puts), count)

    def test_signed_release_requires_and_checks_signature(self):
        files = release_files(signed=True)
        with self.assertRaises(mirror.SyncError):
            self.run_sync(FakeS3(), files)
        self.settings = mirror.settings_from_env({**ENV, "TAURI_PUBLIC_KEY": "fixture-key"})
        s3 = FakeS3()
        verify = self.run_sync(s3, files)
        verify.assert_called_once()
        self.assertTrue(json.loads(s3.objects["cc-switch/latest.json"])["signed"])
        self.assertIn(f"cc-switch/releases/{TAG}/{mirror.SUMS_SIG}", s3.objects)

    def test_checksum_mismatch_stops_before_upload(self):
        files = release_files()
        files["CC-Switch-v4.0.35-Windows.msi"] = b"tampered!\n"[: len(files["CC-Switch-v4.0.35-Windows.msi"])].ljust(
            len(files["CC-Switch-v4.0.35-Windows.msi"]), b"!")
        s3 = FakeS3()
        with self.assertRaises(mirror.SyncError):
            self.run_sync(s3, files)
        self.assertEqual(s3.puts, [])

    def test_existing_different_object_is_never_overwritten(self):
        files = release_files()
        key = f"cc-switch/releases/{TAG}/CC-Switch-v4.0.35-macOS.dmg"
        s3 = FakeS3({key: b"something else"})
        with self.assertRaises(mirror.SyncError):
            self.run_sync(s3, files)
        self.assertEqual(s3.objects[key], b"something else")
        self.assertNotIn("cc-switch/latest.json", s3.objects)

    def test_older_release_never_moves_latest_backwards(self):
        s3 = FakeS3()
        newer = "clean-v4.0.36-r1"
        self.run_sync(s3, release_files(newer), newer)
        self.run_sync(s3, release_files(), TAG)
        self.assertEqual(json.loads(s3.objects["cc-switch/latest.json"])["release_tag"], newer)
        self.assertIn(f"cc-switch/releases/{TAG}/downloads.json", s3.objects)

    def test_revision_ordering(self):
        self.assertLess(mirror.tag_order("clean-v4.0.35-r1"), mirror.tag_order("clean-v4.0.35-r3"))
        self.assertLess(mirror.tag_order("clean-v4.0.35-r9"), mirror.tag_order("clean-v4.1.0-r1"))


if __name__ == "__main__":
    unittest.main()
