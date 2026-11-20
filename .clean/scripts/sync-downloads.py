#!/usr/bin/env python3
"""Verify a signed GitHub clean release, submit it via restricted SFTP, and audit HTTPS.

Runtime dependencies: Python 3.12+, OpenSSH sftp, and minisign. No remote shell,
GitHub writes, third-party Python modules, or provider credentials are used.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SUMS = "SHA256SUMS.txt"
SUMS_SIG = SUMS + ".sig"
PROVENANCE = "clean-build-provenance.json"
JSON_LIMIT = 4 * 1024 * 1024
RECEIPT_LIMIT = 64 * 1024
CHUNK = 1024 * 1024
GITHUB_CDN_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
TAG = re.compile(r"clean-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-r([1-9]\d*)\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,179}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class SyncError(RuntimeError):
    pass


class HttpBoundaryError(SyncError):
    """A redirect/transport identity failure must never become a repair upload."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise SyncError(message)


def integer(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def parse_tag(value: str) -> tuple[str, int, tuple[int, int, int, int]]:
    match = TAG.fullmatch(value) if isinstance(value, str) and len(value) <= 100 else None
    require(match, "Expected a clean-vMAJOR.MINOR.PATCH-rREVISION release tag")
    parts = tuple(int(item) for item in match.groups())
    return ".".join(match.groups()[:3]), parts[3], parts


def repository_name(value: str) -> str:
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value),
            "GITHUB_REPOSITORY must be owner/repository")
    return value


def https_url(value: str) -> urllib.parse.SplitResult:
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
    except (ValueError, TypeError) as error:
        raise SyncError("Invalid HTTPS URL") from error
    require(url.scheme == "https" and url.hostname and not url.username and not url.password
            and not url.fragment and (port is None or 1 <= port <= 65535), "Only HTTPS URLs without credentials/fragments are allowed")
    return url


def base_url(value: str) -> str:
    url = https_url(value)
    hostname = url.hostname.lower()
    labels = hostname.split(".")
    try:
        socket.inet_aton(hostname)  # Also rejects numeric/hex/octal IPv4 aliases.
        numeric_host = True
    except OSError:
        numeric_host = False
    require(url.path in ("/cc-switch", "/cc-switch/") and not url.query and url.port in (None, 443)
            and len(labels) >= 2 and len(hostname) <= 253 and not numeric_host
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            and labels[-1] not in {"localhost", "local"},
            "DOWNLOAD_BASE_URL must use a public DNS name at https://host/cc-switch, without IPs or custom ports")
    return f"https://{hostname}/cc-switch"


def key_material(value: str) -> bytes:
    try:
        text = value.strip()
        raw = text.encode() if text.startswith("untrusted comment:") else base64.b64decode(text, validate=True)
        lines = raw.decode("ascii").strip().splitlines()
        require(len(lines) == 2 and lines[0].startswith("untrusted comment:"), "Invalid Tauri/minisign public key")
        key = base64.b64decode(lines[1], validate=True)
        require(len(key) == 42 and key[:2] in (b"Ed", b"ED"), "Invalid minisign Ed25519 public key")
        return key
    except (ValueError, UnicodeError, AttributeError) as error:
        raise SyncError("Invalid Tauri/minisign public key") from error


def public_key_text(value: str) -> bytes:
    return b"untrusted comment: CC Switch updater public key\n" + base64.b64encode(key_material(value)) + b"\n"


def signature_bytes(value: bytes) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
        lines = raw.decode("ascii").strip().splitlines()
        require(len(lines) == 4 and lines[0].startswith("untrusted comment:")
                and lines[2].startswith("trusted comment:"), "Expected a base64-encoded minisign signature")
        signed = base64.b64decode(lines[1], validate=True)
        comment_signature = base64.b64decode(lines[3], validate=True)
        require(len(signed) == 74 and signed[:2] in (b"Ed", b"ED") and len(comment_signature) == 64,
                "Invalid minisign signature structure")
        return raw
    except (ValueError, UnicodeError) as error:
        raise SyncError("Invalid base64-encoded minisign signature") from error


def verify_minisign(path: Path, signature: bytes, public_key: str) -> None:
    raw = signature_bytes(signature)
    with tempfile.TemporaryDirectory(prefix="cc-switch-signature-") as temporary:
        directory = Path(temporary)
        key_path, signature_path = directory / "public.key", directory / "payload.sig"
        key_path.write_bytes(public_key_text(public_key))
        signature_path.write_bytes(raw)
        try:
            result = subprocess.run(["minisign", "-Vm", str(path), "-p", str(key_path), "-x", str(signature_path)],
                                    capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SyncError(f"Cannot verify {path.name}: minisign is unavailable or timed out") from error
        require(result.returncode == 0, f"Signature verification failed for {path.name}: {result.stderr.strip()[:1000]}")


def json_bytes(raw: bytes, label: str) -> dict:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            require(key not in result, f"Duplicate JSON key in {label}: {key}")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise SyncError(f"Invalid JSON: {label}") from error
    require(isinstance(value, dict), f"Expected a JSON object: {label}")
    return value


def expected_updates(version: str) -> dict[str, str]:
    prefix = f"CC-Switch-v{version}"
    files = {"darwin-aarch64": f"{prefix}-macOS.tar.gz", "darwin-x86_64": f"{prefix}-macOS.tar.gz",
             "windows-x86_64": f"{prefix}-Windows.msi", "windows-aarch64": f"{prefix}-Windows-arm64.msi"}
    for arch, filename_arch in (("x86_64", "x86_64"), ("aarch64", "arm64")):
        for extension, suffix in (("AppImage", ""), ("deb", "-deb"), ("rpm", "-rpm")):
            files[f"linux-{arch}{suffix}"] = f"{prefix}-Linux-{filename_arch}.{extension}"
    return files


def expected_assets(version: str) -> set[str]:
    updates = set(expected_updates(version).values())
    prefix = f"CC-Switch-v{version}"
    return updates | {name + ".sig" for name in updates} | {
        f"{prefix}-Clean-Source.tar.gz", f"{prefix}-macOS.dmg", f"{prefix}-macOS.zip",
        f"{prefix}-Windows-Portable.zip", f"{prefix}-Windows-arm64-Portable.zip",
        "latest.json", PROVENANCE, SUMS, SUMS_SIG,
    }


def parse_checksums(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("ascii")
    except UnicodeError as error:
        raise SyncError("Checksum list must be ASCII") from error
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,179})", line)
        require(match, "Malformed checksum line or unsafe filename")
        digest, name = match.groups()
        require(name not in result and name not in {SUMS, SUMS_SIG, "READY.json"}, "Duplicate or self-referential checksum entry")
        result[name] = digest
    require(result, "Empty checksum list")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_ssh(host: str, port: str, user: str, known_hosts: str) -> tuple[str, int, str]:
    require(re.fullmatch(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
            and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.split(".")),
            "DOWNLOAD_SSH_HOST must be a simple DNS name or IPv4 address")
    require(re.fullmatch(r"[1-9]\d{0,4}", port) and 1 <= int(port) <= 65535, "Invalid DOWNLOAD_SSH_PORT")
    require(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user), "Invalid DOWNLOAD_SSH_USER")
    require("\x00" not in known_hosts and len(known_hosts) <= 256 * 1024, "Invalid known_hosts data")
    expected = host if int(port) == 22 else f"[{host}]:{int(port)}"
    keys = []
    for line in known_hosts.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        require(len(fields) >= 3 and fields[0] == expected,
                "known_hosts must pin only the exact SSH host and port; wildcards, hashed hosts, markers and host lists are forbidden")
        require(fields[1] in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"},
                "Unsupported SSH host key type")
        try:
            decoded = base64.b64decode(fields[2], validate=True)
            require(len(decoded) >= 16 and base64.b64encode(decoded).decode("ascii") == fields[2], "Invalid SSH host key")
        except ValueError as error:
            raise SyncError("Invalid SSH host key") from error
        keys.append(line)
    require(1 <= len(keys) <= 4, "DOWNLOAD_SSH_KNOWN_HOSTS must contain one to four pinned host keys")
    return host, int(port), user


@dataclass(frozen=True)
class Settings:
    repository: str
    tag: str
    token: str
    public_key: str
    base: str
    host: str
    port: int
    user: str
    private_key: str
    known_hosts: str

    @classmethod
    def from_env(cls, tag: str, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        required = ("GH_TOKEN", "GITHUB_REPOSITORY", "TAURI_PUBLIC_KEY", "DOWNLOAD_BASE_URL", "DOWNLOAD_SSH_HOST",
                    "DOWNLOAD_SSH_USER", "DOWNLOAD_SSH_PRIVATE_KEY", "DOWNLOAD_SSH_KNOWN_HOSTS")
        for name in required:
            require(isinstance(env.get(name), str) and env[name].strip(), f"Missing {name}")
        parse_tag(tag)
        key_material(env["TAURI_PUBLIC_KEY"])
        require("\x00" not in env["DOWNLOAD_SSH_PRIVATE_KEY"], "Invalid SSH private key")
        host, port, user = validate_ssh(env["DOWNLOAD_SSH_HOST"], env.get("DOWNLOAD_SSH_PORT") or "22",
                                        env["DOWNLOAD_SSH_USER"], env["DOWNLOAD_SSH_KNOWN_HOSTS"])
        return cls(repository_name(env["GITHUB_REPOSITORY"]), tag, env["GH_TOKEN"], env["TAURI_PUBLIC_KEY"],
                   base_url(env["DOWNLOAD_BASE_URL"]), host, port, user, env["DOWNLOAD_SSH_PRIVATE_KEY"], env["DOWNLOAD_SSH_KNOWN_HOSTS"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class Http:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, url: str, *, kind: str, base: str = "", method: str = "GET", token: str = "", extra: dict | None = None):
        original = url
        mirror = https_url(base) if kind == "mirror" else None
        headers = {"User-Agent": "cc-switch-download-sync", "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
        if extra:
            headers.update(extra)
        require(not any(name.lower() == "authorization" for name in headers), "Unexpected HTTP authorization header")
        if token:
            require(kind == "api", "GitHub authorization may only be sent to the API")
            headers["Authorization"] = "Bearer " + token
        for attempt in range(6):
            try:
                parsed = https_url(url)
            except SyncError as error:
                raise HttpBoundaryError(str(error)) from error
            def boundary(condition, message):
                if not condition:
                    raise HttpBoundaryError(message)
            if kind == "api":
                boundary(parsed.hostname == "api.github.com" and parsed.port in (None, 443) and attempt == 0,
                        "Authenticated GitHub API redirects are forbidden")
            elif kind == "github":
                boundary((attempt == 0 and url == original and parsed.hostname == "github.com" and parsed.port in (None, 443))
                        or (attempt > 0 and parsed.hostname in GITHUB_CDN_HOSTS and parsed.port in (None, 443)),
                        "GitHub asset redirected outside the approved GitHub CDN")
            elif kind == "mirror":
                path = urllib.parse.unquote(parsed.path)
                boundary((parsed.hostname, parsed.port or 443) == (mirror.hostname, mirror.port or 443)
                        and path.startswith(mirror.path + "/") and ".." not in path.split("/"),
                        "Download URL left the configured HTTPS mirror")
            else:
                raise SyncError("Unknown HTTP trust boundary")
            try:
                response = self.opener.open(urllib.request.Request(url, method=method, headers=headers), timeout=60)
            except urllib.error.HTTPError as error:
                if error.code in (301, 302, 303, 307, 308):
                    location = error.headers.get("Location")
                    error.close()
                    boundary(kind != "api" and location, "Authenticated redirects or redirects without a location are forbidden")
                    url = urllib.parse.urljoin(url, location)
                    continue
                code = error.code
                error.close()
                raise SyncError(f"HTTP {code} while requesting {urllib.parse.urlsplit(original).path}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                raise SyncError(f"HTTP request failed for {urllib.parse.urlsplit(original).path}: {error}") from error
            try:
                boundary(response.geturl() == url, "Unexpected implicit HTTP redirect")
                require(response.headers.get("Content-Encoding", "identity").lower() in ("", "identity"), "Compressed HTTP responses are forbidden")
            except SyncError:
                response.close()
                raise
            return response
        raise SyncError("Too many HTTP redirects")

    def small(self, url: str, *, limit: int = JSON_LIMIT, **kwargs) -> bytes:
        with self.open(url, **kwargs) as response:
            require(response.status == 200, "Expected HTTP 200")
            raw = response.read(limit + 1)
            require(len(raw) <= limit, "HTTP response exceeded the allowed size")
            return raw

    def stream(self, url: str, size: int, *, destination: Path | None = None, **kwargs) -> str:
        digest = hashlib.sha256()
        count = 0
        with self.open(url, **kwargs) as response:
            require(response.status == 200, "Expected HTTP 200 for a complete download")
            length = response.headers.get("Content-Length")
            require(length is None or (length.isdecimal() and int(length) == size), "HTTP Content-Length disagrees with the release asset")
            stream = destination.open("xb") if destination else None
            try:
                while block := response.read(min(CHUNK, size - count + 1)):
                    count += len(block)
                    require(count <= size, "Download exceeded the signed release asset size")
                    digest.update(block)
                    if stream:
                        stream.write(block)
            finally:
                if stream:
                    stream.close()
        require(count == size, "Download was truncated")
        return digest.hexdigest()


def validate_channel(channel: dict, settings: Settings) -> None:
    require(type(channel.get("schema")) is int and channel["schema"] == 1 and channel.get("application") == "cc-switch", "Unsupported mirror channel")
    require(channel.get("repository") == settings.repository and channel.get("base_url") == settings.base,
            "Mirror channel repository or base URL does not match")
    require(key_material(channel.get("updater_public_key", "")) == key_material(settings.public_key), "Mirror channel uses a different updater public key")
    require(integer(channel.get("max_batch_bytes"), 1) and integer(channel.get("min_free_bytes"), 1), "Invalid mirror capacity limits")


def github_release(http: Http, settings: Settings) -> dict[str, dict]:
    url = f"https://api.github.com/repos/{settings.repository}/releases/tags/{settings.tag}"
    release = json_bytes(http.small(url, kind="api", token=settings.token), "GitHub release")
    require(release.get("tag_name") == settings.tag and release.get("draft") is False and release.get("prerelease") is False
            and release.get("published_at"), "Only an already published stable clean release can be synchronized")
    version, _, _ = parse_tag(settings.tag)
    assets = release.get("assets")
    require(isinstance(assets, list) and 0 < len(assets) <= 128, "Invalid GitHub release asset list")
    indexed = {}
    for asset in assets:
        require(isinstance(asset, dict), "Invalid GitHub release asset")
        name = asset.get("name", "")
        require(isinstance(name, str) and NAME.fullmatch(name) and name not in indexed, "Unsafe or duplicate release asset name")
        expected = f"https://github.com/{settings.repository}/releases/download/{settings.tag}/{name}"
        require(asset.get("browser_download_url") == expected and asset.get("state") == "uploaded" and integer(asset.get("size"), 1),
                f"Unexpected asset URL, upload state or size: {name}")
        indexed[name] = asset
    require(set(indexed) == expected_assets(version), "Release asset set is incomplete or contains unexpected files")
    return indexed


def validate_provenance(provenance: dict, settings: Settings, checksums: dict[str, str]) -> None:
    version, revision, _ = parse_tag(settings.tag)
    require(type(provenance.get("schema")) is int and provenance["schema"] == 1 and provenance.get("repository") == settings.repository
            and provenance.get("release_tag") == settings.tag and provenance.get("version") == version
            and type(provenance.get("revision")) is int and provenance["revision"] == revision
            and provenance.get("signed_updates") is True, "Release provenance does not match this signed release")
    require(key_material(provenance.get("updater_public_key", "")) == key_material(settings.public_key), "Release provenance has a different signing key")
    require(provenance.get("asset_sha256") == {name: digest for name, digest in checksums.items() if name != PROVENANCE},
            "Provenance asset hashes disagree with the signed checksum list")


def validate_manifest(manifest: dict, settings: Settings, signatures: dict[str, str]) -> None:
    version, _, _ = parse_tag(settings.tag)
    expected = expected_updates(version)
    require(manifest.get("version") == version and isinstance(manifest.get("platforms"), dict)
            and set(manifest["platforms"]) == set(expected), "Updater manifest version or platform set is incorrect")
    for platform, name in expected.items():
        entry = manifest["platforms"][platform]
        require(isinstance(entry, dict) and set(entry) == {"url", "signature"}, "Unexpected updater platform entry")
        require(entry["url"] == f"https://github.com/{settings.repository}/releases/download/{settings.tag}/{name}"
                and entry["signature"] == signatures.get(name), f"Updater URL or signature does not match: {platform}")
        signature_bytes(entry["signature"].encode("ascii"))


@dataclass
class VerifiedRelease:
    settings: Settings
    directory: Path
    sizes: dict[str, int]
    hashes: dict[str, str]
    manifest: dict

    @property
    def checksums_hash(self) -> str:
        return self.hashes[SUMS]


def download_release(http: Http, settings: Settings, channel: dict, directory: Path) -> VerifiedRelease:
    assets = github_release(http, settings)
    total = sum(asset["size"] for asset in assets.values())
    require(total <= channel["max_batch_bytes"], "Release exceeds the mirror's max_batch_bytes")
    require(assets[SUMS]["size"] <= JSON_LIMIT and assets[SUMS_SIG]["size"] <= RECEIPT_LIMIT,
            "Checksum metadata is too large")
    require(all(asset["size"] <= RECEIPT_LIMIT for name, asset in assets.items() if name.endswith(".sig"))
            and assets[PROVENANCE]["size"] <= JSON_LIMIT and assets["latest.json"]["size"] <= JSON_LIMIT,
            "Release JSON/signature metadata is too large")
    directory.mkdir(mode=0o700)
    hashes = {}
    for name in (SUMS, SUMS_SIG):
        asset = assets[name]
        hashes[name] = http.stream(asset["browser_download_url"], asset["size"], destination=directory / name, kind="github")
    verify_minisign(directory / SUMS, (directory / SUMS_SIG).read_bytes(), settings.public_key)
    checksums = parse_checksums((directory / SUMS).read_bytes())
    require(set(checksums) == set(assets) - {SUMS, SUMS_SIG}, "Signed checksum file does not cover the exact release asset set")
    for name in sorted(checksums):
        asset = assets[name]
        digest = http.stream(asset["browser_download_url"], asset["size"], destination=directory / name, kind="github")
        require(digest == checksums[name], f"GitHub asset hash mismatch: {name}")
        hashes[name] = digest
    provenance = json_bytes((directory / PROVENANCE).read_bytes(), PROVENANCE)
    validate_provenance(provenance, settings, checksums)
    manifest = json_bytes((directory / "latest.json").read_bytes(), "GitHub latest.json")
    version, _, _ = parse_tag(settings.tag)
    signatures = {name: (directory / (name + ".sig")).read_text("ascii").strip() for name in set(expected_updates(version).values())}
    validate_manifest(manifest, settings, signatures)
    for name, signature in signatures.items():
        verify_minisign(directory / name, signature.encode("ascii"), settings.public_key)
    return VerifiedRelease(settings, directory, {name: asset["size"] for name, asset in assets.items()}, hashes, manifest)


def sftp_quote(value: str) -> str:
    require(not any(character in value for character in "\r\n\x00"), "Unsafe SFTP path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Sftp:
    def __init__(self, settings: Settings, directory: Path):
        self.settings = settings
        self.directory = directory
        directory.mkdir(mode=0o700)
        private = directory / "identity"
        known = directory / "known_hosts"
        for path, content in ((private, settings.private_key), (known, settings.known_hosts)):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(content.rstrip("\n") + "\n")
        self.command = ["sftp", "-F", "/dev/null", "-P", str(settings.port), "-i", str(private),
                        "-oStrictHostKeyChecking=yes", f"-oUserKnownHostsFile={known}", "-oGlobalKnownHostsFile=/dev/null",
                        "-oIdentitiesOnly=yes", "-oIdentityAgent=none", "-oBatchMode=yes", "-oPasswordAuthentication=no",
                        "-oKbdInteractiveAuthentication=no", "-oPreferredAuthentications=publickey", "-oPermitLocalCommand=no",
                        "-oClearAllForwardings=yes", "-oConnectTimeout=30", "-oServerAliveInterval=30", "-oServerAliveCountMax=3",
                        "-b", "-", f"{settings.user}@{settings.host}"]

    def run(self, batch: str, *, timeout: int = 1800) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(self.command, input=batch, text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SyncError("SFTP could not start or timed out; no remote shell was used") from error

    def upload(self, release: VerifiedRelease, batch_id: str) -> None:
        require(NAME.fullmatch(batch_id) and len(batch_id) <= 180, "Unsafe batch ID")
        remote = "/cc-switch/" + batch_id
        ready = self.directory / "READY.json"
        ready.write_text(json.dumps({"schema": 1, "repository": self.settings.repository, "release_tag": self.settings.tag,
                                    "checksums_sha256": release.checksums_hash, "files_count": len(release.sizes),
                                    "total_bytes": sum(release.sizes.values())}, sort_keys=True) + "\n", encoding="utf-8")
        created = self.run(f"mkdir {sftp_quote(remote)}\n", timeout=60)
        require(created.returncode == 0, f"Cannot create a new SFTP batch: {created.stderr.strip()[:3000]}")
        # We have acknowledged ownership of this new directory, and READY has
        # never been attempted. Cleanup is safe only during this asset phase.
        commands = [f"put {sftp_quote(str(release.directory / name))} {sftp_quote(remote + '/' + name)}"
                    for name in sorted(release.sizes)]
        try:
            result = self.run("\n".join(commands) + "\n")
            require(result.returncode == 0, f"SFTP asset upload failed: {result.stderr.strip()[:3000]}")
        except SyncError:
            cleanup = [f"-rm {sftp_quote(remote + '/' + name)}" for name in sorted(release.sizes)]
            cleanup.append(f"-rmdir {sftp_quote(remote)}")
            try:
                result = self.run("\n".join(cleanup) + "\n", timeout=60)
                if result.returncode:
                    print("Incomplete-batch cleanup failed; the server will expire abandoned batches.", file=sys.stderr)
            except SyncError:
                print("Incomplete-batch cleanup was unavailable; the original upload error is preserved.", file=sys.stderr)
            raise
        # A disconnected READY transfer has an ambiguous result: it may already
        # have reached the publisher. Never remove or modify the batch here.
        result = self.run(f"put {sftp_quote(str(ready))} {sftp_quote(remote + '/READY.json.part')}\n"
                          f"rename {sftp_quote(remote + '/READY.json.part')} {sftp_quote(remote + '/READY.json')}\n", timeout=60)
        require(result.returncode == 0, f"SFTP READY submission was not acknowledged; the batch is preserved: {result.stderr.strip()[:3000]}")

    def receipt(self, batch_id: str, *, timeout: int = 45) -> dict | None:
        require(NAME.fullmatch(batch_id), "Unsafe receipt batch ID")
        remote = f"/receipts/{batch_id}.json"
        local = self.directory / "receipt.json"
        local.unlink(missing_ok=True)
        result = self.run(f"get {sftp_quote(remote)} {sftp_quote(str(local))}\n", timeout=timeout)
        if result.returncode != 0:
            message = result.stderr + "\n" + result.stdout
            missing = remote in message and re.search(r"No such file|not found", message, re.IGNORECASE)
            require(bool(missing) and result.returncode != 255, f"SFTP receipt retrieval failed: {message.strip()[:3000]}")
            return None
        require(local.is_file() and local.stat().st_size <= RECEIPT_LIMIT, "Missing or oversized publisher receipt")
        return json_bytes(local.read_bytes(), "publisher receipt")


def validate_receipt(receipt: dict, release: VerifiedRelease) -> None:
    status = receipt.get("status")
    detail = receipt.get("error") or receipt.get("message") or "Publisher returned an unspecified error"
    mismatch = "Publisher receipt does not identify this exact release/checksum batch"
    if status == "error":
        # A server error before READY could be read has no usable tag/hash. Keep
        # rejecting the unbound receipt, but also preserve its real diagnosis.
        mismatch += f"; publisher reported: {str(detail)[:4000]}"
    require(type(receipt.get("schema")) is int and receipt["schema"] == 1 and receipt.get("repository") == release.settings.repository
            and receipt.get("release_tag") == release.settings.tag and receipt.get("checksums_sha256") == release.checksums_hash,
            mismatch)
    require(status in {"published", "already_published", "superseded", "error"}, "Unknown publisher receipt status")
    if status == "error":
        raise SyncError(f"Publisher rejected this batch: {str(detail)[:4000]}")
    if status == "superseded":
        require(parse_tag(receipt.get("current_tag", ""))[2] > parse_tag(release.settings.tag)[2],
                "Superseded receipt must identify a strictly newer clean release")
    elif "current_tag" in receipt:
        require(receipt["current_tag"] == release.settings.tag, "Receipt current_tag does not match the published release")


def wait_receipt(sftp: Sftp, batch_id: str, release: VerifiedRelease, *, timeout: int = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receipt = sftp.receipt(batch_id, timeout=max(1, min(45, int(deadline - time.monotonic()))))
        if receipt is not None:
            validate_receipt(receipt, release)
            return receipt
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(15, remaining))
    raise SyncError(f"Publisher did not return a receipt within {timeout} seconds; a later run may submit a new batch")


def verify_public_assets(http: Http, release: VerifiedRelease) -> None:
    settings = release.settings
    for name, size in sorted(release.sizes.items()):
        url = f"{settings.base}/releases/{settings.tag}/{name}"
        with http.open(url, kind="mirror", base=settings.base, method="HEAD") as response:
            length = response.headers.get("Content-Length", "")
            require(response.status == 200 and length.isdecimal() and int(length) == size, f"Public HEAD length mismatch: {name}")
        length = min(1024, size)
        with http.open(url, kind="mirror", base=settings.base, extra={"Range": f"bytes=0-{length - 1}"}) as response:
            require(response.status == 206 and response.headers.get("Content-Range") == f"bytes 0-{length - 1}/{size}",
                    f"Public download does not honor byte ranges: {name}")
            sample = response.read(length + 1)
            with (release.directory / name).open("rb") as original:
                require(len(sample) == length and sample == original.read(length), f"Public range content mismatch: {name}")
        digest = http.stream(url, size, kind="mirror", base=settings.base)
        require(digest == release.hashes[name], f"Public full-download SHA-256 mismatch: {name}")
        print(f"Verified complete HTTPS download: {name} ({size} bytes)")


def mapped_manifest(original: dict, base: str, tag: str) -> dict:
    mapped = json.loads(json.dumps(original))
    version, _, _ = parse_tag(tag)
    for platform, name in expected_updates(version).items():
        mapped["platforms"][platform]["url"] = f"{base}/releases/{tag}/{name}"
    return mapped


def superseding_manifest(http: Http, release: VerifiedRelease, tag: str, directory: Path) -> dict:
    """Authenticate the newer stable manifest without trusting an unsigned receipt alone."""
    settings = Settings(**{**vars(release.settings), "tag": tag})
    prefix = f"{settings.base}/releases/{tag}/"
    sums = http.small(prefix + SUMS, kind="mirror", base=settings.base)
    signature = http.small(prefix + SUMS_SIG, limit=RECEIPT_LIMIT, kind="mirror", base=settings.base)
    path = directory / "current-SHA256SUMS.txt"
    path.write_bytes(sums)
    verify_minisign(path, signature, settings.public_key)
    checksums = parse_checksums(sums)
    version, _, _ = parse_tag(tag)
    require(set(checksums) == expected_assets(version) - {SUMS, SUMS_SIG}, "Newer stable release checksum set is incomplete")
    values = {}
    signatures = {}
    for name in [PROVENANCE, "latest.json", *sorted({name + ".sig" for name in expected_updates(version).values()})]:
        raw = http.small(prefix + name, kind="mirror", base=settings.base)
        require(hashlib.sha256(raw).hexdigest() == checksums[name], f"Newer stable signed metadata hash mismatch: {name}")
        if name.endswith(".sig"):
            signatures[name[:-4]] = raw.decode("ascii").strip()
        else:
            values[name] = json_bytes(raw, "newer stable " + name)
    validate_provenance(values[PROVENANCE], settings, checksums)
    validate_manifest(values["latest.json"], settings, signatures)
    return values["latest.json"]


def verify_stable(http: Http, release: VerifiedRelease, receipt: dict, directory: Path) -> None:
    tag = receipt["current_tag"] if receipt["status"] == "superseded" else release.settings.tag
    original = superseding_manifest(http, release, tag, directory) if tag != release.settings.tag else release.manifest
    stable = json_bytes(http.small(release.settings.base + "/stable/latest.json", kind="mirror", base=release.settings.base), "stable/latest.json")
    require(stable == mapped_manifest(original, release.settings.base, tag), "Stable updater manifest differs from the authenticated release or changes its signatures")


def batch_identifier(tag: str, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    run, attempt = env.get("GITHUB_RUN_ID", "local"), env.get("GITHUB_RUN_ATTEMPT", "1")
    require(re.fullmatch(r"(?:local|[0-9]{1,24})", run) and re.fullmatch(r"[1-9][0-9]{0,5}", attempt), "Unsafe GitHub run identifier")
    identifier = f"{tag}-{run}-{attempt}-{secrets.token_hex(4)}"
    require(NAME.fullmatch(identifier), "Unsafe batch identifier")
    return identifier


def synchronize(http: Http, release: VerifiedRelease, directory: Path) -> str:
    # A scheduled retry of a healthy immutable release does not create another
    # 500 MB inbox batch. This still downloads and hashes every public asset.
    try:
        verify_public_assets(http, release)
        verify_stable(http, release, {"status": "already_published"}, directory)
    except HttpBoundaryError:
        raise
    except SyncError as error:
        print(f"Existing mirror did not pass complete verification: {error}")
        print("Submitting a verified batch; complete HTTPS verification is required again after the publisher receipt.")
    else:
        print(f"Existing mirror release {release.settings.tag} passed complete verification; no SFTP upload is needed.")
        return "already_verified"
    batch_id = batch_identifier(release.settings.tag)
    sftp = Sftp(release.settings, directory / "ssh")
    print(f"Submitting verified release {release.settings.tag} as batch {batch_id}")
    sftp.upload(release, batch_id)
    receipt = wait_receipt(sftp, batch_id, release)
    verify_public_assets(http, release)
    verify_stable(http, release, receipt, directory)
    return receipt["status"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    settings = Settings.from_env(args.tag)
    require(shutil.which("sftp") and shutil.which("minisign"), "Install OpenSSH sftp and minisign before running sync-downloads")
    http = Http()
    channel = json_bytes(http.small(settings.base + "/channel.json", kind="mirror", base=settings.base), "channel.json")
    validate_channel(channel, settings)
    with tempfile.TemporaryDirectory(prefix="cc-switch-sync-") as temporary:
        directory = Path(temporary)
        release = download_release(http, settings, channel, directory / "assets")
        status = synchronize(http, release, directory)
        print(f"Verified mirror publication: {settings.base}/releases/{settings.tag}/ ({status})")
        print("All published assets passed complete streamed SHA-256 checks; updater signatures and stable URLs were verified.")


if __name__ == "__main__":
    try:
        main()
    except (SyncError, OSError, UnicodeError) as error:
        print(f"Download synchronization failed: {error}", file=sys.stderr)
        raise SystemExit(1)
