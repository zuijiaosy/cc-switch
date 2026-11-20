#!/usr/bin/env python3
"""Publish signed CC Switch SFTP batches. Requires Python 3.10+ and minisign.

The SFTP user never writes the public tree or this program. Uploaded files are
copied through no-follow descriptors into private storage before verification.
The server holds only a public key, never the Tauri or SSH private keys.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _protocol import (CHUNK, PROVENANCE, SUMS, SUMS_SIG, PublishError,
                       expected_assets, expected_updates, json_bytes, key_material,
                       parse_checksums, public_key_text, require, sha256,
                       verify_minisign)

TAG_TEXT = r"clean-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-r([1-9]\d*)"
TAG = re.compile(TAG_TEXT + r"\Z")
BATCH = re.compile(TAG_TEXT + r"-(?:local|[0-9]{1,24})-[1-9][0-9]{0,5}-[0-9a-f]{8}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
JSON_LIMIT = 4 * 1024 * 1024
SMALL_LIMIT = 64 * 1024
MAX_FILES = 128
EXPIRE_SECONDS = 24 * 60 * 60
DEFAULT_MAX_BATCH = 2 * 1024**3
DEFAULT_MIN_FREE = 4 * 1024**3


def integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def parse_tag(value: object) -> tuple[str, int, tuple[int, int, int, int]]:
    match = TAG.fullmatch(value) if isinstance(value, str) else None
    require(match, "Expected clean-vMAJOR.MINOR.PATCH-rREVISION")
    parts = tuple(int(item) for item in match.groups())
    return ".".join(match.groups()[:3]), parts[3], parts


def normalize_base(value: object) -> str:
    require(isinstance(value, str), "base_url must be a string")
    try:
        url = urllib.parse.urlsplit(value)
        host, port = url.hostname, url.port
    except ValueError as error:
        raise PublishError("Invalid base_url") from error
    require(url.scheme == "https" and host and not url.username and not url.password
            and not url.query and not url.fragment and url.path in ("/cc-switch", "/cc-switch/")
            and port in (None, 443), "base_url must be https://public-domain/cc-switch")
    require(host.split(".")[-1] not in ("localhost", "local") and "." in host
            and len(host) <= 253 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                                        for part in host.split(".")), "base_url must use a public DNS hostname")
    try:
        socket.inet_aton(host)  # Includes shortened, hexadecimal and octal IPv4 aliases.
    except OSError:
        pass
    else:
        raise PublishError("base_url must not use an IP address")
    return f"https://{host.lower()}/cc-switch"


def open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def trusted_path(path: Path, *, directory: bool, publisher_only: bool = False) -> None:
    info = path.lstat()
    allowed_owners = {os.geteuid()} if publisher_only else {0, os.geteuid()}
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    require(expected_type(info.st_mode) and info.st_uid in allowed_owners and info.st_mode & 0o022 == 0,
            f"Unsafe owner or group/world-write permissions on trusted {'directory' if directory else 'file'}: {path}")


def read_regular(directory: Path, name: str, limit: int) -> bytes:
    descriptor = open_directory(directory)
    try:
        return read_at(descriptor, name, limit)
    finally:
        os.close(descriptor)


def read_at(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f"Not a regular unlinked file: {name}")
        require(0 < info.st_size <= limit, f"Metadata size is invalid: {name}")
        result = stream.read(limit + 1)
        require(len(result) == info.st_size and len(result) <= limit, f"Metadata changed or exceeded limit: {name}")
        return result


def write_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sync_directory(path: Path) -> None:
    descriptor = open_directory(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Publisher:
    def __init__(self, configuration: dict):
        self.config = configuration
        self.base = normalize_base(configuration.get("base_url"))
        self.repository = configuration.get("repository", "")
        require(isinstance(self.repository, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", self.repository),
                "repository must be owner/repository")
        root = configuration.get("root", "/srv/client-downloads")
        require(isinstance(root, str) and Path(root).is_absolute() and root != "/", "root must be an absolute dedicated directory")
        self.root = Path(root)
        self.max_batch = configuration.get("max_batch_bytes", DEFAULT_MAX_BATCH)
        self.min_free = configuration.get("min_free_bytes", DEFAULT_MIN_FREE)
        require(integer(self.max_batch, 1) and integer(self.min_free, 1), "Capacity limits must be positive integers")
        key_file = configuration.get("public_key_file", "")
        require(isinstance(key_file, str) and Path(key_file).is_absolute(), "public_key_file must be absolute")
        path = Path(key_file)
        trusted_path(path, directory=False)
        trusted_path(path.parent, directory=True)
        self.public_key = read_regular(path.parent, path.name, SMALL_LIMIT).decode("ascii")
        self.public_key = base64.b64encode(public_key_text(self.public_key)).decode("ascii")
        self.inbox = self.root / "incoming/cc-switch"
        self.receipts = self.root / "incoming/receipts"
        self.work = self.root / "work/cc-switch"
        self.public = self.root / "public/cc-switch"
        self.releases = self.public / "releases"

    def check_layout(self) -> None:
        for path in (self.root, self.root / "incoming", self.inbox, self.receipts,
                     self.root / "work", self.work, self.root / "public", self.public):
            require(path.is_dir() and not path.is_symlink(), f"Missing or unsafe deployment directory: {path}")
        for path in (self.root, self.root / "incoming", self.root / "work", self.root / "public"):
            trusted_path(path, directory=True)
        for path in (self.public, self.receipts):
            trusted_path(path, directory=True, publisher_only=True)
        require(self.inbox.stat().st_mode & 0o002 == 0, "Upload inbox must not be world-writable")
        require(stat.S_IMODE(self.work.stat().st_mode) == 0o700 and self.work.stat().st_uid == os.geteuid(),
                "Private work directory must be owned by publisher with mode 0700")
        if not os.path.lexists(self.releases):
            self.releases.mkdir(mode=0o755)
            os.chmod(self.releases, 0o755)
        require(not self.releases.is_symlink() and self.releases.is_dir(), "Unsafe releases directory")
        trusted_path(self.releases, directory=True, publisher_only=True)
        require(self.work.stat().st_dev == self.releases.stat().st_dev,
                "Private work and public releases must be on the same filesystem for atomic publication")

    @contextlib.contextmanager
    def locked(self):
        self.check_layout()
        descriptor = os.open(self.work / ".publisher.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def channel(self) -> dict:
        return {"schema": 1, "application": "cc-switch", "repository": self.repository,
                "base_url": self.base, "updater_public_key": self.public_key,
                "max_batch_bytes": self.max_batch, "min_free_bytes": self.min_free}

    def check_channel(self) -> None:
        current = json_bytes(read_regular(self.public, "channel.json", JSON_LIMIT), "channel.json")
        expected = self.channel()
        require(current == expected, "Public channel.json does not match trusted configuration; run --write-channel")

    def write_channel(self) -> None:
        with self.locked():
            if (self.public / "channel.json").exists():
                old = json_bytes(read_regular(self.public, "channel.json", JSON_LIMIT), "channel.json")
                require(old.get("repository") == self.repository and old.get("base_url") == self.base
                        and key_material(old.get("updater_public_key", "")) == key_material(self.public_key),
                        "Refusing to change an established channel's repository, domain or signing identity")
            atomic_file(self.public / "channel.json", encoded(self.channel()))
            index = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                     '<meta name="viewport" content="width=device-width,initial-scale=1">'
                     '<title>CC Switch 下载</title><h1>CC Switch 下载</h1>'
                     '<p><a href="stable/">查看已验证的最新稳定版本</a></p>'
                     '<p>首次发布完成前，稳定版本页面尚不可用。</p></html>')
            atomic_file(self.public / "index.html", index.encode())

    def capacity(self, additional: int) -> None:
        require(shutil.disk_usage(self.work).free >= additional + self.min_free,
                f"Insufficient free disk space: need additional {additional} bytes plus reserve {self.min_free}; published releases are preserved")

    def copy_payload(self, directory: int, name: str, destination: Path, remaining: int) -> tuple[int, str]:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f"Not a regular unlinked file: {name}")
            require(0 < info.st_size <= remaining, f"Asset exceeds batch byte budget: {name}")
            if name.endswith(".sig"):
                require(info.st_size <= SMALL_LIMIT, f"Oversized signature: {name}")
            if name in (PROVENANCE, "latest.json"):
                require(info.st_size <= JSON_LIMIT, f"Oversized release JSON: {name}")
            self.capacity(info.st_size)
            digest, count = hashlib.sha256(), 0
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                while block := source.read(min(CHUNK, info.st_size - count + 1)):
                    count += len(block)
                    require(count <= info.st_size and count <= remaining, f"Asset grew while copying: {name}")
                    target.write(block)
                    digest.update(block)
                require(count == info.st_size, f"Asset truncated while copying: {name}")
                target.flush()
                os.fsync(target.fileno())
            return count, digest.hexdigest()

    def validate_snapshot(self, snapshot: Path, tag: str, checksums: dict[str, str]) -> dict:
        version, revision, _ = parse_tag(tag)
        provenance = json_bytes(read_regular(snapshot, PROVENANCE, JSON_LIMIT), PROVENANCE)
        require(type(provenance.get("schema")) is int and provenance["schema"] == 1
                and provenance.get("repository") == self.repository and provenance.get("release_tag") == tag
                and provenance.get("version") == version and type(provenance.get("revision")) is int
                and provenance["revision"] == revision and provenance.get("signed_updates") is True,
                "Provenance does not identify this signed repository/version/revision")
        require(key_material(provenance.get("updater_public_key", "")) == key_material(self.public_key),
                "Provenance signing identity differs from trusted server public key")
        require(provenance.get("asset_sha256") == {name: digest for name, digest in checksums.items() if name != PROVENANCE},
                "Provenance hashes disagree with signed checksum list")
        manifest = json_bytes(read_regular(snapshot, "latest.json", JSON_LIMIT), "latest.json")
        updates = expected_updates(version)
        require(manifest.get("version") == version and isinstance(manifest.get("platforms"), dict)
                and set(manifest["platforms"]) == set(updates), "Updater manifest version or complete platform set is invalid")
        signatures = {}
        for name in sorted(set(updates.values())):
            signature = read_regular(snapshot, name + ".sig", SMALL_LIMIT)
            verify_minisign(snapshot / name, signature, self.public_key)
            signatures[name] = signature.decode("ascii").strip()
        for platform, name in updates.items():
            entry = manifest["platforms"][platform]
            require(isinstance(entry, dict) and set(entry) == {"url", "signature"}
                    and entry["url"] == f"https://github.com/{self.repository}/releases/download/{tag}/{name}"
                    and entry["signature"] == signatures[name], f"Updater URL/signature mismatch: {platform}")
        return manifest

    def current_tag(self) -> str | None:
        stable = self.public / "stable"
        if not os.path.lexists(stable):
            return None
        require(stable.is_symlink(), "Stable channel is not the managed symbolic link")
        target = os.readlink(stable)
        parts = target.split("/")
        require(len(parts) == 3 and parts[0] == "releases" and parts[2] == "channel", "Unsafe stable symbolic link")
        parse_tag(parts[1])
        require((self.releases / parts[1]).is_dir() and not (self.releases / parts[1]).is_symlink()
                and (self.releases / parts[1] / "channel").is_dir()
                and not (self.releases / parts[1] / "channel").is_symlink(), "Stable channel target is missing or unsafe")
        return parts[1]

    def derive_channel(self, snapshot: Path, tag: str, manifest: dict, checksums: dict) -> None:
        channel = snapshot / "channel"
        channel.mkdir(mode=0o755)
        os.chmod(channel, 0o755)
        derived = json.loads(json.dumps(manifest))
        version, _, _ = parse_tag(tag)
        for platform, name in expected_updates(version).items():
            derived["platforms"][platform]["url"] = f"{self.base}/releases/{tag}/{name}"
        write_file(channel / "latest.json", encoded(derived))
        write_file(channel / "index.html", self.download_page(tag, checksums))
        sync_directory(channel)

    def download_page(self, tag: str, checksums: dict) -> bytes:
        version, revision, _ = parse_tag(tag)
        prefix = f"CC-Switch-v{version}"
        def link(name, label, primary=False):
            require(name in checksums or name in (SUMS, SUMS_SIG), f"Download page refers to an unsigned asset: {name}")
            style = ' class="button primary"' if primary else ' class="button"'
            return f'<a{style} href="{html.escape(self.base)}/releases/{tag}/{name}">{html.escape(label)}</a>'
        groups = [
            ("macOS", "Apple Silicon 与 Intel 通用", [("macOS.dmg", "下载 DMG 安装包"), ("macOS.zip", "下载 ZIP 应用包")]),
            ("Windows x64", "Intel / AMD 64 位电脑", [("Windows.msi", "下载 MSI 安装包"), ("Windows-Portable.zip", "下载便携 ZIP")]),
            ("Windows ARM64", "Windows on ARM 电脑", [("Windows-arm64.msi", "下载 ARM64 MSI"), ("Windows-arm64-Portable.zip", "下载 ARM64 便携 ZIP")]),
            ("Linux x64", "Intel / AMD 64 位发行版", [("Linux-x86_64.AppImage", "下载 AppImage"), ("Linux-x86_64.deb", "下载 DEB"), ("Linux-x86_64.rpm", "下载 RPM")]),
            ("Linux ARM64", "ARM 64 位发行版", [("Linux-arm64.AppImage", "下载 ARM64 AppImage"), ("Linux-arm64.deb", "下载 ARM64 DEB"), ("Linux-arm64.rpm", "下载 ARM64 RPM")]),
        ]
        cards = "\n".join('<section class="card"><h2>' + title + '</h2><p class="muted">' + detail + '</p><div class="downloads">'
                          + "".join(link(prefix + "-" + suffix, label, index == 0) for index, (suffix, label) in enumerate(assets))
                          + '</div></section>' for title, detail, assets in groups)
        metadata = "".join(link(name, label) for name, label in (
            (SUMS, "SHA-256 校验清单"), (SUMS_SIG, "校验清单签名"),
            (PROVENANCE, "构建来源与文件哈希"), (prefix + "-Clean-Source.tar.gz", "完整构建源码")))
        signatures = "".join('<li>' + link(name, name) + '</li>' for name in sorted(checksums) if name.endswith(".sig"))
        updater = link(prefix + "-macOS.tar.gz", "macOS 通用自动更新包")
        page = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CC Switch VERSION · 本站推荐构建下载</title>
<style>
:root{color-scheme:light dark;--bg:#f5f7fb;--card:#fff;--text:#152033;--muted:#596579;--line:#dce2eb;--accent:#1655cf;--button:#edf3ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1120px;margin:auto;padding:56px 24px}header{max-width:850px;margin-bottom:32px}.eyebrow{font-size:13px;font-weight:700;letter-spacing:.1em;color:var(--accent)}h1{font-size:clamp(28px,5vw,42px);line-height:1.2;margin:12px 0 20px}h2{font-size:21px;margin:0 0 6px}p{margin:10px 0}.muted,footer{color:var(--muted)}.tags{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}.tags span{border:1px solid var(--line);border-radius:6px;padding:4px 9px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:18px}.card,.verification{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}.downloads{display:flex;flex-direction:column;align-items:flex-start;gap:10px;margin-top:22px}a{color:var(--accent);text-underline-offset:3px}.button{display:inline-block;border:1px solid var(--line);border-radius:8px;padding:9px 14px;text-decoration:none;background:var(--button);font-size:14px;overflow-wrap:anywhere}.primary{color:#fff;background:var(--accent);border-color:var(--accent);font-weight:600}a:focus-visible,summary:focus-visible{outline:3px solid #f29b38;outline-offset:4px}.verification{margin-top:28px}.metadata{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}summary{cursor:pointer}details{margin-top:18px}details ul{padding-left:20px}details li{margin:8px 0}footer{margin-top:28px;font-size:14px}.sources{display:flex;gap:20px;flex-wrap:wrap}
@media(prefers-color-scheme:dark){:root{--bg:#111722;--card:#192230;--text:#e7edf7;--muted:#a4b1c4;--line:#334156;--accent:#86adff;--button:#202e44}.primary{background:#346bdb;border-color:#346bdb}}
@media(max-width:480px){main{padding:32px 16px}.card,.verification{padding:20px}.grid{grid-template-columns:1fr}}
</style></head><body><main><header><div class="eyebrow">本站推荐构建</div>
<h1>CC Switch VERSION</h1>
<p>移除官方内置供应商推荐与广告，仅保留 <strong>CodexZH</strong> 与 <strong>CcodeZH</strong> 两个自有供应商预设。API Key 由使用者自行填写，其他应用功能沿用上游。</p>
<p class="muted">这是基于 CC Switch 的衍生构建，并非 CC Switch 官方发布。请按系统和处理器架构选择安装包。</p>
<div class="tags"><span>上游版本 vVERSION</span><span>清理修订 rREVISION</span><span>TAG</span></div>
</header><div class="grid">CARDS</div>
<section class="verification"><h2>校验与构建来源</h2><p class="muted">所有文件保留 GitHub 发行版的原始字节。校验清单及更新包均使用本项目自己的签名密钥；完整源码与构建记录可一并下载。</p>
<div class="metadata">METADATA</div><details><summary>自动更新包与独立签名文件</summary><p>UPDATER</p><ul>SIGNATURES</ul></details></section>
<footer><p>应用内部版本为 VERSION。同一上游版本的清理修订变化需要手动覆盖安装；后续上游版本通过本域名的签名更新通道分发。</p>
<div class="sources"><a href="https://github.com/farion1231/cc-switch/releases/tag/vVERSION">上游 Release</a><a href="https://github.com/REPOSITORY">本项目仓库</a><a href="https://github.com/REPOSITORY/releases/tag/TAG">对应 GitHub 发行版</a></div></footer>
</main></body></html>'''
        substitutions = {"SIGNATURES": signatures, "METADATA": metadata, "UPDATER": updater, "CARDS": cards,
                         "REPOSITORY": html.escape(self.repository), "VERSION": version, "REVISION": str(revision), "TAG": tag}
        page = re.sub(r"SIGNATURES|METADATA|UPDATER|CARDS|REPOSITORY|VERSION|REVISION|TAG",
                      lambda match: substitutions[match.group()], page)
        return page.encode("utf-8")

    def promote(self, tag: str) -> None:
        pending = self.public / (".stable-" + os.urandom(8).hex())
        try:
            os.symlink(f"releases/{tag}/channel", pending)
            os.replace(pending, self.public / "stable")
            sync_directory(self.public)
        finally:
            pending.unlink(missing_ok=True)

    def publish_snapshot(self, snapshot: Path, tag: str, checksums_hash: str, checksums: dict, manifest: dict) -> tuple[str, str]:
        current = self.current_tag()
        destination = self.releases / tag
        existed = os.path.lexists(destination)
        if existed:
            require(destination.is_dir() and not destination.is_symlink(), "Existing release path is unsafe")
            trusted_path(destination, directory=True, publisher_only=True)
            trusted_path(destination / "channel", directory=True, publisher_only=True)
            require(hashlib.sha256(read_regular(destination, SUMS, JSON_LIMIT)).hexdigest() == checksums_hash,
                    "Same release tag has different signed checksums; immutable releases cannot be overwritten")
            expected = set(checksums) | {SUMS, SUMS_SIG, "channel"}
            require(set(os.listdir(destination)) == expected, "Existing immutable release file set is damaged")
            for name, digest in checksums.items():
                # Use a private source; never repair/overwrite existing public bytes.
                descriptor = open_directory(destination)
                try:
                    trusted_path(destination / name, directory=False, publisher_only=True)
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                            and sha256(destination / name) == digest, f"Existing immutable release is damaged: {name}")
                finally:
                    os.close(descriptor)
            verify_minisign(destination / SUMS, read_regular(destination, SUMS_SIG, SMALL_LIMIT), self.public_key)
            require(read_regular(destination, SUMS_SIG, SMALL_LIMIT) == read_regular(snapshot, SUMS_SIG, SMALL_LIMIT),
                    "Same release tag has different checksum-signature bytes; immutable releases cannot be overwritten")
            expected_derived = json.loads(json.dumps(manifest))
            for platform, name in expected_updates(parse_tag(tag)[0]).items():
                expected_derived["platforms"][platform]["url"] = f"{self.base}/releases/{tag}/{name}"
            require(json_bytes(read_regular(destination / "channel", "latest.json", JSON_LIMIT), "derived latest.json") == expected_derived,
                    "Existing derived updater manifest is damaged")
        else:
            self.derive_channel(snapshot, tag, manifest, checksums)
            for item in snapshot.iterdir():
                if item.is_file():
                    os.chmod(item, 0o644)
            os.chmod(snapshot, 0o755)
            sync_directory(snapshot)
            os.rename(snapshot, destination)
            sync_directory(self.releases)
        if current is not None and parse_tag(current)[2] > parse_tag(tag)[2]:
            return "superseded", current
        if current != tag:
            self.promote(tag)
        return ("already_published" if existed else "published"), tag

    def receipt(self, batch: str, ready: dict, status: str, message: str, current: str | None = None) -> dict:
        result = {"schema": 1, "repository": self.repository, "release_tag": ready.get("release_tag", ""),
                  "checksums_sha256": ready.get("checksums_sha256", ""), "status": status, "message": message[:4000]}
        if current is not None:
            result["current_tag"] = current
        atomic_file(self.receipts / (batch + ".json"), encoded(result), 0o640)
        return result

    def remove_batch(self, batch: str, identity: tuple[int, int]) -> None:
        require(BATCH.fullmatch(batch) and len(batch) <= 180, "Unsafe cleanup batch name")
        descriptor = open_directory(self.inbox)
        try:
            try:
                info = os.stat(batch, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            require((info.st_dev, info.st_ino) == identity, "Batch directory was replaced; refusing cleanup")
            if stat.S_ISLNK(info.st_mode):
                os.unlink(batch, dir_fd=descriptor)
            else:
                # Python 3.10 already has symlink-resistant internal fd traversal;
                # its public rmtree API does not yet accept a dir_fd parameter.
                require(shutil.rmtree.avoids_symlink_attacks, "This platform lacks safe directory cleanup")
                shutil.rmtree(self.inbox / batch)
        finally:
            os.close(descriptor)

    def process(self, batch: str) -> dict:
        require(BATCH.fullmatch(batch) and len(batch) <= 180, "Unsafe batch name")
        directory = self.inbox / batch
        descriptor = open_directory(directory)
        info = os.fstat(descriptor)
        identity = info.st_dev, info.st_ino
        ready, temporary, result = {}, None, None
        try:
            ready = json_bytes(read_at(descriptor, "READY.json", SMALL_LIMIT), "READY.json")
            tag = ready.get("release_tag")
            version, _, _ = parse_tag(tag)
            require(batch.startswith(tag + "-"), "Batch name does not match release tag")
            require(type(ready.get("schema")) is int and ready["schema"] == 1 and ready.get("repository") == self.repository,
                    "READY repository/schema is invalid")
            require(isinstance(ready.get("checksums_sha256"), str) and SHA256.fullmatch(ready["checksums_sha256"]), "Invalid READY checksum digest")
            require(integer(ready.get("files_count"), 1) and ready["files_count"] <= MAX_FILES
                    and integer(ready.get("total_bytes"), 1) and ready["total_bytes"] <= self.max_batch,
                    "READY file count or total bytes exceeds batch budget")
            require(set(os.listdir(descriptor)) == expected_assets(version) | {"READY.json"},
                    "Batch contains missing, extra or non-platform files")
            require(ready["files_count"] == len(expected_assets(version)), "READY file count does not match complete platform set")
            self.capacity(ready["total_bytes"])
            temporary = Path(tempfile.mkdtemp(prefix=".publish-", dir=self.work))
            sums, sums_signature = read_at(descriptor, SUMS, JSON_LIMIT), read_at(descriptor, SUMS_SIG, SMALL_LIMIT)
            require(hashlib.sha256(sums).hexdigest() == ready["checksums_sha256"], "READY checksum digest mismatch")
            require(len(sums) + len(sums_signature) <= ready["total_bytes"], "Metadata exceeds READY byte budget")
            write_file(temporary / SUMS, sums, 0o600)
            write_file(temporary / SUMS_SIG, sums_signature, 0o600)
            verify_minisign(temporary / SUMS, sums_signature, self.public_key)
            checksums = parse_checksums(sums)
            require(set(checksums) == expected_assets(version) - {SUMS, SUMS_SIG}, "Signed checksum set does not cover every required platform and metadata file")
            count = len(sums) + len(sums_signature)
            for name, expected in sorted(checksums.items()):
                size, digest = self.copy_payload(descriptor, name, temporary / name, min(self.max_batch, ready["total_bytes"]) - count)
                require(digest == expected, f"Signed asset hash mismatch: {name}")
                count += size
            require(count == ready["total_bytes"], "READY total bytes does not match uploaded assets")
            require(set(os.listdir(descriptor)) == expected_assets(version) | {"READY.json"}, "Batch file set changed during snapshot")
            manifest = self.validate_snapshot(temporary, tag, checksums)
            self.capacity(0)
            status, current = self.publish_snapshot(temporary, tag, ready["checksums_sha256"], checksums, manifest)
            result = self.receipt(batch, ready, status, "All files, signatures and release metadata verified; immutable download files are available", current)
        except (PublishError, OSError, UnicodeError, ValueError) as error:
            result = self.receipt(batch, ready, "error", str(error))
        finally:
            os.close(descriptor)
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
        # Once a terminal receipt exists, release inbox quota even for rejection.
        # If receipt persistence fails, leave input intact for a retry instead.
        self.remove_batch(batch, identity)
        return result

    def expire_incomplete(self, now: float) -> None:
        for path in self.inbox.iterdir():
            if not BATCH.fullmatch(path.name) or len(path.name) > 180:
                continue
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or now - info.st_mtime <= EXPIRE_SECONDS:
                continue
            descriptor = open_directory(path)
            try:
                if "READY.json" in os.listdir(descriptor):
                    continue
                # Avoid expiring a still active transfer into a day-old directory.
                if any(now - os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mtime <= EXPIRE_SECONDS
                       for name in os.listdir(descriptor)):
                    continue
            finally:
                os.close(descriptor)
            self.remove_batch(path.name, (info.st_dev, info.st_ino))
        for path in self.work.iterdir():
            info = path.lstat()
            if re.fullmatch(r"\.publish-[A-Za-z0-9_-]+", path.name) and stat.S_ISDIR(info.st_mode) and now - info.st_mtime > EXPIRE_SECONDS:
                shutil.rmtree(path)

    def once(self) -> list[dict]:
        with self.locked():
            self.check_channel()
            self.expire_incomplete(time.time())
            results = []
            for path in sorted(self.inbox.iterdir()):
                if not BATCH.fullmatch(path.name) or len(path.name) > 180 or path.is_symlink() or not path.is_dir():
                    continue
                if os.path.lexists(path / "READY.json"):
                    result = self.process(path.name)
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write-channel", action="store_true")
    modes.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    try:
        trusted_path(arguments.config, directory=False)
        trusted_path(arguments.config.parent, directory=True)
        configuration = json_bytes(read_regular(arguments.config.parent, arguments.config.name, JSON_LIMIT), "trusted server config")
        publisher = Publisher(configuration)
        if arguments.write_channel:
            publisher.write_channel()
            print("Wrote public channel identity; no unsigned release or updater manifest was published")
            return 0
        results = publisher.once()
        return int(any(result["status"] == "error" for result in results))
    except (PublishError, OSError, UnicodeError, ValueError) as error:
        print(f"CC Switch publisher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
