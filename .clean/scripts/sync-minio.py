#!/usr/bin/env python3
"""Mirror a published clean GitHub release into MinIO for direct downloads.

Layout under <bucket>/<prefix>/ (prefix defaults to "cc-switch"):

    releases/<clean-tag>/<asset>   immutable installers, checksums, provenance
    releases/<clean-tag>/downloads.json
    releases/<clean-tag>/LICENSE.txt   upstream MIT notice taken from the verified source archive
    latest.json                    download manifest read by the static site

Only installers and verification files are mirrored; Tauri updater-only assets
(per-file .sig, latest.json, macOS updater tarball) stay on GitHub. Every file
is checked against the release's SHA256SUMS.txt (and its minisign signature
when the release is signed) before upload, and again over public HTTPS after
upload. Existing objects are never overwritten with different content, and
latest.json never moves to an older release.

Runtime dependencies: Python 3.12+, the AWS CLI v2 (preinstalled on GitHub's
Ubuntu runners), gh, and minisign for signed releases.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

_SPEC = importlib.util.spec_from_file_location("clean_sync_downloads", Path(__file__).with_name("sync-downloads.py"))
_shared = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _shared
_SPEC.loader.exec_module(_shared)

SyncError = _shared.SyncError
require = _shared.require
parse_tag = _shared.parse_tag
parse_checksums = _shared.parse_checksums
verify_minisign = _shared.verify_minisign
sha256 = _shared.sha256
SUMS, SUMS_SIG, PROVENANCE = _shared.SUMS, _shared.SUMS_SIG, _shared.PROVENANCE

MANIFEST = "latest.json"
LICENSE = "LICENSE.txt"
RELEASE_MANIFEST = "downloads.json"
CHUNK = 1024 * 1024
IMMUTABLE = "public, max-age=31536000, immutable"
MUTABLE = "no-cache, max-age=0, must-revalidate"
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
PREFIX = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")

# (filename suffix after "CC-Switch-vX.Y.Z-", os, arch, kind)
KINDS = (
    ("Windows.msi", "windows", "x64", "msi"),
    ("Windows-Portable.zip", "windows", "x64", "portable"),
    ("Windows-arm64.msi", "windows", "arm64", "msi"),
    ("Windows-arm64-Portable.zip", "windows", "arm64", "portable"),
    ("macOS.dmg", "macos", "universal", "dmg"),
    ("macOS.zip", "macos", "universal", "zip"),
    ("Linux-x86_64.AppImage", "linux", "x64", "appimage"),
    ("Linux-x86_64.deb", "linux", "x64", "deb"),
    ("Linux-x86_64.rpm", "linux", "x64", "rpm"),
    ("Linux-arm64.AppImage", "linux", "arm64", "appimage"),
    ("Linux-arm64.deb", "linux", "arm64", "deb"),
    ("Linux-arm64.rpm", "linux", "arm64", "rpm"),
    ("Clean-Source.tar.gz", "source", "any", "source"),
)
REQUIRED_KINDS = {(os_, arch, kind) for _, os_, arch, kind in KINDS}
CONTENT_TYPES = {
    ".msi": "application/x-msi",
    ".zip": "application/zip",
    ".dmg": "application/x-apple-diskimage",
    ".AppImage": "application/octet-stream",
    ".deb": "application/vnd.debian.binary-package",
    ".rpm": "application/x-rpm",
    ".gz": "application/gzip",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".sig": "text/plain; charset=utf-8",
}


def tag_order(tag: str) -> tuple[int, int, int, int]:
    return parse_tag(tag)[2]


def content_type(name: str) -> str:
    for suffix, value in CONTENT_TYPES.items():
        if name.endswith(suffix):
            return value
    return "application/octet-stream"


def classify(name: str, upstream_tag: str) -> dict | None:
    prefix = f"CC-Switch-{upstream_tag}-"
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix):]
    for suffix, os_, arch, kind in KINDS:
        if rest == suffix:
            return {"os": os_, "arch": arch, "kind": kind}
    return None


def mirrored_names(names: set[str], upstream_tag: str) -> list[str]:
    """Installers plus the verification files; updater-only assets are skipped."""
    selected = {name for name in names if classify(name, upstream_tag)}
    missing = REQUIRED_KINDS - {tuple(classify(name, upstream_tag).values()) for name in selected}
    require(not missing, f"Release is missing platform installers: {sorted(missing)}")
    for name in (SUMS, PROVENANCE):
        require(name in names, f"Release is missing {name}")
        selected.add(name)
    if SUMS_SIG in names:
        selected.add(SUMS_SIG)
    return sorted(selected)


@dataclass(frozen=True)
class Settings:
    repository: str
    endpoint: str
    bucket: str
    prefix: str
    public_base: str
    public_key: str

    @property
    def root(self) -> str:
        return f"{self.public_base}/{self.prefix}"

    def key(self, *parts: str) -> str:
        return "/".join((self.prefix, *parts))

    def public_url(self, *parts: str) -> str:
        return "/".join((self.root, *(urllib.parse.quote(part) for part in parts)))


def https_base(value: str, label: str) -> str:
    url = urllib.parse.urlsplit(value.strip())
    require(url.scheme == "https" and url.hostname and not url.username and not url.password
            and not url.query and not url.fragment, f"{label} must be an https:// URL without credentials")
    return f"https://{url.netloc.lower()}{url.path.rstrip('/')}"


def settings_from_env(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    repository = _shared.repository_name(env.get("GITHUB_REPOSITORY", ""))
    endpoint = https_base(env.get("MINIO_ENDPOINT", ""), "MINIO_ENDPOINT")
    require(urllib.parse.urlsplit(endpoint).path == "", "MINIO_ENDPOINT must not contain a path")
    bucket = env.get("MINIO_BUCKET", "").strip()
    require(BUCKET.fullmatch(bucket) and ".." not in bucket, "MINIO_BUCKET is not a valid bucket name")
    prefix = (env.get("MINIO_PREFIX") or "cc-switch").strip()
    require(PREFIX.fullmatch(prefix), "MINIO_PREFIX must be a single lowercase path segment")
    public = env.get("MINIO_PUBLIC_BASE_URL", "").strip()
    public_base = https_base(public, "MINIO_PUBLIC_BASE_URL") if public else f"{endpoint}/{bucket}"
    for name in ("MINIO_ACCESS_KEY_ID", "MINIO_SECRET_ACCESS_KEY"):
        require(env.get(name, "").strip(), f"{name} is required")
    return Settings(repository, endpoint, bucket, prefix, public_base, env.get("TAURI_PUBLIC_KEY", "").strip())


class S3:
    """Thin AWS CLI wrapper; credentials are passed only through the environment."""

    def __init__(self, settings: Settings, env: dict | None = None):
        env = dict(os.environ if env is None else env)
        self.settings = settings
        self.env = {
            **env,
            "AWS_ACCESS_KEY_ID": env["MINIO_ACCESS_KEY_ID"].strip(),
            "AWS_SECRET_ACCESS_KEY": env["MINIO_SECRET_ACCESS_KEY"].strip(),
            "AWS_DEFAULT_REGION": env.get("MINIO_REGION") or "us-east-1",
            # MinIO RELEASE.2024-12 predates the CLI's newer default checksums.
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_PAGER": "",
        }
        self.env.pop("AWS_SESSION_TOKEN", None)
        self.env.pop("AWS_PROFILE", None)
        with tempfile.NamedTemporaryFile("w", prefix="cc-switch-aws-", suffix=".ini", delete=False) as stream:
            # Cloudflare in front of the endpoint rejects request bodies over
            # 100 MB, so every larger file must go as multipart parts.
            stream.write("[default]\ns3 =\n  addressing_style = path\n"
                         "  multipart_threshold = 32MB\n  multipart_chunksize = 32MB\n  max_concurrent_requests = 4\n")
            self.config = stream.name
        self.env["AWS_CONFIG_FILE"] = self.config
        self.env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull

    def close(self) -> None:
        Path(self.config).unlink(missing_ok=True)

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        command = ["aws", "--endpoint-url", self.settings.endpoint, "--no-cli-pager", *args]
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=1800, check=False)
        if check and result.returncode != 0:
            raise SyncError(f"aws {args[0]} {args[1]} failed: {result.stderr.strip()[-800:]}")
        return result

    def list(self, prefix: str) -> dict[str, int]:
        """Object sizes under a prefix. Deliberately not HeadObject: Cloudflare
        forwards HEAD for cacheable extensions (.zip, .dmg, .gz…) to the origin
        as GET, which breaks the SigV4 signature and returns 403."""
        result = self.run("s3api", "list-objects-v2", "--bucket", self.settings.bucket, "--prefix", prefix,
                          "--output", "json")
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        return {item["Key"]: item["Size"] for item in data.get("Contents") or []}

    def get(self, key: str, destination: Path) -> bool:
        result = self.run("s3api", "get-object", "--bucket", self.settings.bucket, "--key", key, str(destination), check=False)
        if result.returncode != 0:
            if re.search(r"NoSuchKey|\(404\)|Not Found", result.stderr):
                return False
            raise SyncError(f"Cannot read s3://{self.settings.bucket}/{key}: {result.stderr.strip()[-800:]}")
        return True

    def put(self, path: Path, key: str, *, cache_control: str, digest: str, attachment: bool) -> None:
        args = ["s3", "cp", str(path), f"s3://{self.settings.bucket}/{key}", "--only-show-errors",
                "--content-type", content_type(path.name), "--cache-control", cache_control,
                "--metadata", f"sha256={digest}"]
        if attachment:
            args += ["--content-disposition", f'attachment; filename="{path.name}"']
        self.run(*args)


def http_get(url: str, *, headers: dict | None = None, attempts: int = 5) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "cc-switch-minio-mirror", **(headers or {})})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 404 or attempt == attempts - 1:
                raise SyncError(f"GET {url} returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts - 1:
                raise SyncError(f"GET {url} failed: {error}") from error
        time.sleep(min(30, 5 * (attempt + 1)))
    raise SyncError(f"GET {url} failed")


def public_digest(url: str, attempts: int = 5) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "cc-switch-minio-mirror"})
    for attempt in range(attempts):
        try:
            digest, size = hashlib.sha256(), 0
            with urllib.request.urlopen(request, timeout=120) as response:
                for block in iter(lambda: response.read(CHUNK), b""):
                    digest.update(block)
                    size += len(block)
            return size, digest.hexdigest()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == attempts - 1:
                raise SyncError(f"Public download failed: {url}: {error}") from error
            time.sleep(min(30, 5 * (attempt + 1)))
    raise SyncError(f"Public download failed: {url}")


def gh_json(path: str) -> dict:
    result = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                            capture_output=True, text=True, timeout=120, check=False)
    require(result.returncode == 0, f"GitHub API {path} failed: {result.stderr.strip()[-500:]}")
    return json.loads(result.stdout)


def resolve_release(repository: str, tag: str | None) -> dict:
    release = gh_json(f"/repos/{repository}/releases/tags/{tag}" if tag else f"/repos/{repository}/releases/latest")
    require(not release.get("draft") and not release.get("prerelease"), "Only a published stable release can be mirrored")
    parse_tag(release["tag_name"])
    require(tag is None or release["tag_name"] == tag, "GitHub returned a different release tag")
    return release


def download_assets(repository: str, tag: str, names: list[str], directory: Path) -> None:
    args = ["gh", "release", "download", tag, "--repo", repository, "--dir", str(directory), "--clobber"]
    for name in names:
        args += ["--pattern", name]
    result = subprocess.run(args, capture_output=True, text=True, timeout=3600, check=False)
    require(result.returncode == 0, f"Cannot download release assets: {result.stderr.strip()[-800:]}")


def verify_local(directory: Path, names: list[str], sizes: dict[str, int], settings: Settings) -> dict[str, str]:
    checksums = parse_checksums((directory / SUMS).read_bytes())
    if SUMS_SIG in names:
        require(settings.public_key, "Signed release requires TAURI_PUBLIC_KEY to verify SHA256SUMS.txt.sig")
        verify_minisign(directory / SUMS, (directory / SUMS_SIG).read_bytes(), settings.public_key)
    digests = {}
    for name in names:
        path = directory / name
        require(path.is_file() and path.stat().st_size == sizes[name], f"Downloaded size mismatch: {name}")
        digests[name] = sha256(path)
        if name not in (SUMS, SUMS_SIG):
            require(checksums.get(name) == digests[name], f"SHA-256 mismatch against {SUMS}: {name}")
    return digests


def extract_license(directory: Path, upstream_tag: str) -> Path:
    """Ship the upstream copyright and MIT permission notice next to the
    installers, taken from the checksum-verified clean source archive."""
    archive = directory / f"CC-Switch-{upstream_tag}-Clean-Source.tar.gz"
    with tarfile.open(archive, "r:gz") as stream:
        member = next((item for item in stream.getmembers()
                       if item.isfile() and item.name.lstrip("./") == "LICENSE"), None)
        require(member is not None and member.size < 64 * 1024, "Clean source archive has no top-level LICENSE")
        text = stream.extractfile(member).read()
    require(b"MIT License" in text and b"Permission is hereby granted" in text and b"Copyright" in text,
            "Upstream LICENSE is not the expected MIT notice")
    path = directory / LICENSE
    path.write_bytes(text)
    return path


def build_manifest(release: dict, settings: Settings, names: list[str], sizes: dict[str, int],
                   digests: dict[str, str], provenance: dict) -> dict:
    tag = release["tag_name"]
    version, revision, _ = parse_tag(tag)
    upstream_tag = f"v{version}"
    require(provenance.get("release_tag") == tag and provenance.get("repository") == settings.repository,
            "Build provenance does not describe this release")
    files = []
    for name in names:
        kind = classify(name, upstream_tag)
        if not kind:
            continue
        files.append({**kind, "name": name, "size": sizes[name], "sha256": digests[name],
                      "url": settings.public_url("releases", tag, name)})
    return {
        "schema": 1,
        "application": "cc-switch",
        "repository": settings.repository,
        "upstream_repository": provenance.get("upstream_repository", "farion1231/cc-switch"),
        "release_tag": tag,
        "version": version,
        "revision": revision,
        "published_at": release.get("published_at"),
        "release_url": release.get("html_url"),
        "signed": SUMS_SIG in names,
        "macos_notarized": bool(provenance.get("macos_notarized")),
        "checksums_url": settings.public_url("releases", tag, SUMS),
        "license": "MIT",
        "license_url": settings.public_url("releases", tag, LICENSE),
        "files": files,
    }


def current_manifest(s3: S3, settings: Settings, directory: Path) -> dict | None:
    path = directory / "current-latest.json"
    if not s3.get(settings.key(MANIFEST), path):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        parse_tag(value["release_tag"])
        return value
    except (ValueError, KeyError, TypeError, SyncError):
        print("::warning::Existing MinIO latest.json is unreadable; it will be replaced")
        return None


def upload_release(s3: S3, settings: Settings, tag: str, directory: Path, names: list[str],
                   sizes: dict[str, int], digests: dict[str, str]) -> int:
    uploaded = 0
    existing = s3.list(settings.key("releases", tag) + "/")
    for name in names:
        key = settings.key("releases", tag, name)
        if key in existing:
            same = existing[key] == sizes[name] and public_digest(settings.public_url("releases", tag, name)) == (sizes[name], digests[name])
            require(same, f"Refusing to overwrite different existing object: {key}")
            continue
        attachment = name not in (SUMS, SUMS_SIG, PROVENANCE, LICENSE)
        s3.put(directory / name, key, cache_control=IMMUTABLE, digest=digests[name], attachment=attachment)
        uploaded += 1
        print(f"Uploaded {key} ({sizes[name]} bytes)")
    return uploaded


def verify_public(settings: Settings, tag: str, names: list[str], sizes: dict[str, int], digests: dict[str, str]) -> None:
    for name in names:
        size, digest = public_digest(settings.public_url("releases", tag, name))
        require(size == sizes[name] and digest == digests[name], f"Public download does not match release: {name}")
    largest = max(names, key=lambda item: sizes[item])
    status, headers, body = http_get(settings.public_url("releases", tag, largest), headers={"Range": "bytes=0-1023"})
    require(status == 206 and len(body) == 1024, f"Range download is not supported for {largest}")
    print(f"Verified {len(names)} public downloads under {settings.public_url('releases', tag)}/")


def publish_manifest(s3: S3, settings: Settings, manifest: dict, directory: Path, *, promote: bool) -> None:
    tag = manifest["release_tag"]
    body = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path = directory / RELEASE_MANIFEST
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    key = settings.key("releases", tag, RELEASE_MANIFEST)
    if key not in s3.list(key):
        s3.put(path, key, cache_control=IMMUTABLE, digest=digest, attachment=False)
    elif public_digest(settings.public_url("releases", tag, RELEASE_MANIFEST))[1] != digest:
        print(f"::warning::{key} already exists with different content; kept the original")
    if not promote:
        return
    s3.put(path, settings.key(MANIFEST), cache_control=MUTABLE, digest=digest, attachment=False)
    for attempt in range(6):
        _, _, raw = http_get(settings.public_url(MANIFEST))
        if json.loads(raw).get("release_tag") == tag:
            print(f"Published download manifest {settings.public_url(MANIFEST)} -> {tag}")
            return
        time.sleep(10)
    raise SyncError("Public latest.json does not show the mirrored release")


def already_mirrored(s3: S3, settings: Settings, tag: str, names: list[str], sizes: dict[str, int]) -> bool:
    existing = s3.list(settings.key("releases", tag) + "/")
    for name in names + [RELEASE_MANIFEST, LICENSE]:
        size = existing.get(settings.key("releases", tag, name))
        if size is None or (name in sizes and size != sizes[name]):
            return False
    return True


def synchronize(settings: Settings, s3: S3, tag: str | None) -> None:
    release = resolve_release(settings.repository, tag)
    tag = release["tag_name"]
    sizes = {asset["name"]: asset["size"] for asset in release["assets"]}
    version = parse_tag(tag)[0]
    names = mirrored_names(set(sizes), f"v{version}")
    with tempfile.TemporaryDirectory(prefix="cc-switch-minio-") as temporary:
        directory = Path(temporary)
        current = current_manifest(s3, settings, directory)
        promote = current is None or tag_order(tag) >= tag_order(current["release_tag"])
        if not promote:
            print(f"MinIO already serves newer {current['release_tag']}; {tag} is mirrored without moving latest.json")
        if current and current.get("release_tag") == tag and already_mirrored(s3, settings, tag, names, sizes):
            print(f"MinIO already mirrors {tag}; nothing to upload")
            return
        download_assets(settings.repository, tag, names, directory)
        digests = verify_local(directory, names, sizes, settings)
        provenance = json.loads((directory / PROVENANCE).read_text(encoding="utf-8"))
        manifest = build_manifest(release, settings, names, sizes, digests, provenance)
        license_path = extract_license(directory, f"v{version}")
        sizes[LICENSE], digests[LICENSE] = license_path.stat().st_size, sha256(license_path)
        published = names + [LICENSE]
        uploaded = upload_release(s3, settings, tag, directory, published, sizes, digests)
        verify_public(settings, tag, published, sizes, digests)
        publish_manifest(s3, settings, manifest, directory, promote=promote)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(f"### MinIO mirror\n\n- Release: `{tag}`\n- Uploaded objects: {uploaded}\n"
                             f"- Manifest: {settings.public_url(MANIFEST)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="", help="clean release tag; defaults to the latest published release")
    args = parser.parse_args()
    try:
        settings = settings_from_env()
        s3 = S3(settings)
        try:
            synchronize(settings, s3, args.tag.strip() or None)
        finally:
            s3.close()
    except SyncError as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
