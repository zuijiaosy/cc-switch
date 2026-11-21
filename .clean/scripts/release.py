#!/usr/bin/env python3
"""Prepare and publish reproducible downstream releases without third-party Python deps.

The upstream application version is preserved. Clean-only revisions require a
manual reinstall; only a newer upstream version participates in SemVer updates.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(".clean-state")
API = "https://api.github.com"
PLATFORMS = ("macos", "windows-x64", "windows-arm64", "linux-x64", "linux-arm64")
GITHUB_PROPAGATION_DELAYS = (0, 3, 8, 15, 30, 45)
TAURI_SIGNER_PACKAGE = "@tauri-apps/cli@2.12.4"


def fail(message: str) -> None:
    raise SystemExit(message)


def config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def read_state(directory: Path = STATE) -> dict:
    return json.loads((directory / "release.json").read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def api(path: str, *, anonymous: bool = False, allow_404: bool = False) -> object | None:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "cc-switch-clean-release"}
    if not anonymous:
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            fail("GH_TOKEN is required for repository API requests")
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(API + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and allow_404:
            return None
        fail(f"GitHub API {path}: HTTP {error.code}")


def find_release(repository: str, tag: str) -> dict | None:
    """Find a published release or an authenticated draft with this exact tag.

    GitHub's release-by-tag endpoint does not return drafts. Drafts also need not
    have a Git tag yet, so find them in the paginated release collection and read
    their canonical resource by ID instead of trying to resolve a Git ref.
    """
    found = api(f"/repos/{repository}/releases/tags/{tag}", allow_404=True)
    if found is not None:
        return found
    page = 1
    while True:
        releases = api(f"/repos/{repository}/releases?per_page=100&page={page}")
        if not isinstance(releases, list):
            fail("Unexpected GitHub release collection response")
        matches = [item for item in releases if item.get("tag_name") == tag]
        if len(matches) > 1:
            fail(f"Multiple releases found for tag {tag}")
        if matches:
            release_id = matches[0]["id"]
            if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id < 1:
                fail("Unexpected GitHub release ID")
            found = api(f"/repos/{repository}/releases/{release_id}")
            if found.get("tag_name") != tag:
                fail("GitHub release tag changed during lookup")
            return found
        if len(releases) < 100:
            return None
        page += 1


def wait_for_created_draft(repository: str, tag: str, target: str) -> dict:
    """The release collection may lag a successful draft creation."""
    for delay in GITHUB_PROPAGATION_DELAYS:
        if delay:
            time.sleep(delay)
        draft = find_release(repository, tag)
        if draft is None:
            continue
        if (
            draft.get("draft") is not True
            or draft.get("tag_name") != tag
            or draft.get("target_commitish") != target
        ):
            fail("Created draft has unexpected provenance. Upload stopped.")
        return draft
    fail(f"Created draft {tag} is still not visible. Rerun the publish job to resume from the existing build artifacts.")


def has_signed_release_history(repository: str) -> bool:
    """A previous signed release or draft makes missing setup a real failure.

    Read the authenticated, paginated collection, not just the current latest
    release: latest may have been moved back to an unsigned older build. A
    manifest or signature alone is enough evidence, including incomplete drafts.
    API failures must propagate; they are not proof of an uninitialized channel.
    """
    page = 1
    while True:
        releases = api(f"/repos/{repository}/releases?per_page=100&page={page}")
        if not isinstance(releases, list):
            fail("Unexpected GitHub release collection response during setup detection")
        for item in releases:
            if not item.get("tag_name", "").startswith("clean-"):
                continue
            for asset in item.get("assets", []):
                name = asset.get("name", "")
                if name == "latest.json" or name.endswith(".sig"):
                    return True
        if len(releases) < 100:
            return False
        page += 1


def output(name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        fail("Refusing a multiline workflow output")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")
    print(f"{name}={value}")


def report_setup_required(repository: str) -> None:
    output("build", "false")
    output("state", "setup_required")
    print("::notice::签名更新尚未完成首次初始化，本次不构建、不发布。请在已登录 GitHub CLI 的本机运行 node .clean/scripts/setup-updates.mjs。")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"""## 等待首次初始化

签名自动更新**尚未启用**：当前没有配置更新公钥、私钥或更新端点，也没有发现带更新清单或签名附件的历史清理版（含草稿）。

本次自动检查**没有构建、发布或安装新版本**，没有更改现有发行版。等待首次配置是正常等待状态，不代表更新通道已经可用。

请在已登录 GitHub CLI 的本机进入本仓库目录，执行一次：

```sh
node .clean/scripts/setup-updates.mjs
```

初始化脚本会完成已授权的公开与独立签名配置并触发构建。只有构建、签名和公开下载验证全部通过，才能认定自动更新已启用。

[初始化说明](https://github.com/{repository}/blob/main/.clean/README.md)
""")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_public_key(value: str) -> str:
    text = value.strip()
    if text.startswith("untrusted comment:"):
        raw = text.encode("utf-8")
    else:
        try:
            raw = base64.b64decode(text, validate=True)
        except ValueError:
            fail("TAURI_PUBLIC_KEY must contain a Tauri/minisign public key")
    lines = raw.decode("utf-8").strip().splitlines()
    if len(lines) != 2 or not lines[0].startswith("untrusted comment:"):
        fail("TAURI_PUBLIC_KEY is not a two-line minisign public key")
    try:
        key = base64.b64decode(lines[1], validate=True)
    except ValueError:
        fail("TAURI_PUBLIC_KEY has invalid base64 key material")
    if len(key) != 42 or key[:2] not in (b"Ed", b"ED"):
        fail("TAURI_PUBLIC_KEY is not a minisign Ed25519 public key")
    return base64.b64encode(raw).decode("ascii")


def public_key_material(value: str) -> bytes:
    encoded = normalize_public_key(value)
    lines = base64.b64decode(encoded).decode("utf-8").strip().splitlines()
    return base64.b64decode(lines[1], validate=True)


def stable_version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        fail(f"Expected a stable application version, received {value!r}")
    return tuple(int(part) for part in value.split("."))


def public_json(url: str, *, same_origin: bool = False) -> object:
    if not url.startswith("https://"):
        raise RuntimeError("Public update resources must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "cc-switch-clean-release", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not response.geturl().startswith("https://"):
            raise RuntimeError("Public resource redirected to non-HTTPS transport")
        if same_origin:
            requested = urllib.parse.urlsplit(url)
            received = urllib.parse.urlsplit(response.geturl())
            if (received.hostname, received.port or 443) != (requested.hostname, requested.port or 443):
                raise RuntimeError("Download channel redirected outside its configured HTTPS origin")
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("Public update JSON exceeded the expected size limit")
        return json.loads(raw)


def normalize_download_base(value: str) -> str:
    """Canonical public download origin; the server protocol fixes /cc-switch."""
    if not isinstance(value, str):
        fail("DOWNLOAD_BASE_URL must be an HTTPS URL")
    value = value.strip()
    if not value:
        return ""
    value = value.rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        fail("DOWNLOAD_BASE_URL is not a valid HTTPS URL")
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or parsed.path != "/cc-switch" or "?" in value or "#" in value
            or any(character.isspace() or ord(character) < 32 for character in value)):
        fail("DOWNLOAD_BASE_URL must be https://<public-domain>/cc-switch without credentials, query, fragment, or a non-443 port")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        fail("DOWNLOAD_BASE_URL must use a public DNS name, not an IP address")
    host = host.lower()
    labels = host.split(".")
    if (host == "localhost" or host.endswith((".localhost", ".local")) or len(labels) < 2
            or len(host) > 253 or labels[-1].isdigit()
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        fail("DOWNLOAD_BASE_URL must use a public DNS name, not localhost or an invalid hostname")
    return f"https://{host}/cc-switch"


def github_update_endpoint(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or repository.lower() == "farion1231/cc-switch":
        fail("The updater repository must be the downstream owner/name repository")
    return f"https://github.com/{repository}/releases/latest/download/latest.json"


def expected_updater_endpoints(data: dict) -> list[str]:
    """Validate recorded endpoints, including legacy provenance with one string."""
    if not data["signed_updates"]:
        if data.get("updater_endpoint") or data.get("updater_endpoints") or data.get("download_base_url"):
            fail("Unsigned builds cannot configure update or download endpoints")
        return []
    github = github_update_endpoint(data["repository"])
    base = normalize_download_base(data.get("download_base_url", ""))
    expected = [f"{base}/stable/latest.json", github] if base else [github]
    if data.get("updater_endpoint") != expected[0] or data.get("updater_endpoints", expected) != expected:
        fail("Updater endpoints do not match the configured download channel and this repository's GitHub fallback")
    return expected


def check_download_readiness(repository: str, public_key: str, base: str) -> None:
    """Validate public server identity without exposing SSH credentials to builds."""
    host = os.environ.get("DOWNLOAD_SSH_HOST", "").strip()
    port = os.environ.get("DOWNLOAD_SSH_PORT", "").strip() or "22"
    user = os.environ.get("DOWNLOAD_SSH_USER", "").strip()
    if (not host or len(host) > 253
            or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in host.split("."))):
        fail("DOWNLOAD_SSH_HOST must be a simple DNS name or IPv4 address")
    if not re.fullmatch(r"[1-9]\d{0,4}", port) or not 1 <= int(port) <= 65535:
        fail("DOWNLOAD_SSH_PORT must be between 1 and 65535")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        fail("DOWNLOAD_SSH_USER must be the dedicated SFTP account")
    if (os.environ.get("HAS_DOWNLOAD_SSH_KEY", "false").lower() != "true"
            or os.environ.get("HAS_DOWNLOAD_KNOWN_HOSTS", "false").lower() != "true"):
        fail("Download setup is incomplete: configure DOWNLOAD_SSH_PRIVATE_KEY and pinned DOWNLOAD_SSH_KNOWN_HOSTS before building")
    try:
        channel = public_json(f"{base}/channel.json", same_origin=True)
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as error:
        fail(f"Download channel preflight failed: {error}")
    if not isinstance(channel, dict) or type(channel.get("schema")) is not int or channel["schema"] != 1 or channel.get("application") != "cc-switch":
        fail("Download channel.json has an unsupported schema or application")
    if channel.get("repository") != repository or channel.get("base_url") != base:
        fail("Download channel.json repository or base URL does not match this distribution")
    channel_key = channel.get("updater_public_key")
    if not isinstance(channel_key, str) or public_key_material(channel_key) != public_key_material(public_key):
        fail("Download channel.json uses a different updater public key")
    for name, minimum in (("max_batch_bytes", 1), ("min_free_bytes", 1)):
        if type(channel.get(name)) is not int or channel[name] < minimum:
            fail(f"Download channel.json has an invalid {name}")
    print(f"Verified download server identity and configured capacity limits: {base}/channel.json")


def public_asset_size(url: str) -> int | None:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "cc-switch-clean-release"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200 or not response.geturl().startswith("https://"):
            raise RuntimeError("Anonymous update asset request failed")
        size = response.headers.get("Content-Length")
        return int(size) if size is not None else None


def check_release_continuity(data: dict) -> None:
    """Keep installed clients on a monotonic version stream and the same key."""
    previous = api(f"/repos/{data['repository']}/releases/latest", allow_404=True)
    if not previous or not previous["tag_name"].startswith("clean-"):
        return
    names = [asset for asset in previous["assets"] if asset["name"] == "clean-build-provenance.json"]
    if len(names) != 1:
        fail("Previous clean release lacks unambiguous provenance; review before publishing")
    expected = f"https://github.com/{data['repository']}/releases/download/{previous['tag_name']}/clean-build-provenance.json"
    if names[0]["browser_download_url"] != expected:
        fail("Previous provenance URL is outside this repository release")
    provenance = public_json(expected)
    if provenance.get("repository") != data["repository"] or provenance.get("release_tag") != previous["tag_name"]:
        fail("Previous release provenance does not match the destination repository/tag")
    old_version, new_version = stable_version(provenance["version"]), stable_version(data["version"])
    if old_version > new_version:
        fail("Refusing to move the update feed to an older application version")
    if old_version == new_version and provenance["revision"] > data["revision"]:
        fail("Refusing to replace a newer clean revision with an older revision")
    if provenance.get("signed_updates"):
        if not data["signed_updates"]:
            fail("Refusing to replace a signed update feed with an unsigned release")
        if public_key_material(provenance["updater_public_key"]) != public_key_material(data["updater_public_key"]):
            fail("Updater public key differs from the previous signed release; existing clients cannot trust it. Keep the original signing key.")
        previous_base = normalize_download_base(provenance.get("download_base_url", ""))
        if previous_base and normalize_download_base(data.get("download_base_url", "")) != previous_base:
            fail("The previously published primary download domain cannot be removed or changed silently. Existing clients may keep receiving an old valid feed there; a separate channel migration is required.")


def resolve_commit(repository: str, tag: str) -> str:
    data = api(f"/repos/{repository}/git/ref/tags/{tag}")
    obj = data["object"]
    for _ in range(6):
        if obj["type"] == "commit":
            if not re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
                fail("Unexpected upstream commit SHA")
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api(f"/repos/{repository}/git/tags/{obj['sha']}")["object"]
    fail("Could not resolve upstream release tag to a commit")


def detect() -> None:
    cfg = config()
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        fail("Unexpected destination repository name")
    upstream = cfg["upstream_repository"]
    if upstream != "farion1231/cc-switch":
        fail("The clean patch is audited only for farion1231/cc-switch")
    revision = cfg["release_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        fail("release_revision must be a positive integer")
    public_key = os.environ.get("TAURI_PUBLIC_KEY", "").strip()
    endpoint = os.environ.get("CLEAN_UPDATE_ENDPOINT", "").strip()
    has_key = os.environ.get("HAS_SIGNING_KEY", "false").lower() == "true"
    if cfg.get("require_signed_updates", False) and not (public_key and has_key):
        untouched_setup = not public_key and not endpoint and not has_key
        automatic_check = os.environ.get("GITHUB_EVENT_NAME") in ("schedule", "push")
        if untouched_setup and automatic_check and not has_signed_release_history(repository):
            report_setup_required(repository)
            return
        fail("This distribution requires signed automatic updates. Complete .clean/scripts/setup-updates.mjs once on the owner's authenticated computer to configure TAURI_PUBLIC_KEY and TAURI_SIGNING_PRIVATE_KEY; unsigned fallback is forbidden.")
    download_base = normalize_download_base(os.environ.get("DOWNLOAD_BASE_URL", ""))
    release = api(f"/repos/{upstream}/releases/latest")
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        fail("Only an official stable vMAJOR.MINOR.PATCH release may be built")
    clean_tag = f"clean-{tag}-r{revision}"
    existing = find_release(repository, clean_tag)
    signed = bool(public_key or endpoint or has_key or download_base)
    if signed and not (public_key and has_key):
        fail("Partial updater configuration: set TAURI_PUBLIC_KEY variable and TAURI_SIGNING_PRIVATE_KEY secret together")
    expected_endpoint = github_update_endpoint(repository)
    endpoints = []
    if signed:
        public_key = normalize_public_key(public_key)
        primary = f"{download_base}/stable/latest.json" if download_base else expected_endpoint
        # setup-updates.mjs may have already recorded the original GitHub feed.
        # A configured mirror takes priority without requiring that variable's removal.
        if endpoint and endpoint not in (expected_endpoint, primary):
            fail("CLEAN_UPDATE_ENDPOINT conflicts with DOWNLOAD_BASE_URL or this repository's GitHub release feed")
        endpoint = primary
        endpoints = [primary, expected_endpoint] if download_base else [expected_endpoint]
        if download_base:
            check_download_readiness(repository, public_key, download_base)
        public_repo = api(f"/repos/{repository}", anonymous=True, allow_404=True)
        if not public_repo or public_repo.get("private", True):
            fail("Signed native updates require publicly readable release assets. Complete the authorized one-time setup before building; this release will not fall back to manual-only or official updates.")
        print("Signed updater mode: own download domain with same-repository GitHub fallback" if download_base else "Signed updater mode: own public GitHub release feed and own signing key")
    else:
        endpoint = ""
        print("::warning::Manual-download mode: no updater signing key/feed configured; native updater has no endpoints and cannot install the official build.")

    data = {
        "schema": 1,
        "repository": repository,
        "upstream_repository": upstream,
        "upstream_tag": tag,
        "upstream_release_url": release["html_url"],
        "version": tag[1:],
        "release_tag": clean_tag,
        "revision": revision,
        "automation_sha": os.environ["GITHUB_SHA"],
        "signed_updates": signed,
        "updater_endpoint": endpoint,
        "updater_endpoints": endpoints,
        "updater_public_key": public_key,
        "download_base_url": download_base,
    }
    if signed:
        check_release_continuity(data)
    if existing and not existing["draft"]:
        if signed:
            verify_public_release(data)
        print(f"Already published: {clean_tag}; GitHub feed checked. Any configured download mirror is verified in its separate job. Increment release_revision for a new clean-only build.")
        output("build", "false")
        output("state", "up_to_date")
        output("release_tag", clean_tag)
        output("signed_updates", str(signed).lower())
        output("download_enabled", str(bool(download_base)).lower())
        return
    commit = resolve_commit(upstream, tag)
    data["upstream_sha"] = commit
    write_json(STATE / "release.json", data)
    output("build", "true")
    output("state", "build_required")
    output("upstream_sha", commit)
    output("upstream_tag", tag)
    output("release_tag", clean_tag)
    output("signed_updates", str(signed).lower())
    output("download_enabled", str(bool(download_base)).lower())


def configure(source: Path) -> None:
    data = read_state()
    package = json.loads((source / "package.json").read_text(encoding="utf-8"))
    path = source / "src-tauri/tauri.conf.json"
    tauri = json.loads(path.read_text(encoding="utf-8"))
    if package["version"] != data["version"] or tauri["version"] != data["version"]:
        fail("Upstream release tag and application versions disagree")
    updater = tauri["plugins"]["updater"]
    updater["endpoints"] = expected_updater_endpoints(data)
    if data["signed_updates"]:
        updater["pubkey"] = data["updater_public_key"]
    tauri["bundle"]["createUpdaterArtifacts"] = data["signed_updates"]
    write_json(path, tauri)
    write_json(source / "clean-build-provenance.json", data)


def test(source: Path) -> None:
    source = source.resolve(strict=True)
    tests = config()["test_paths"]
    if not tests or not all(isinstance(p, str) and (source / p).is_file() for p in tests):
        fail("Configured regression tests are missing; review the upstream change before publishing")
    vitest_config = source / "vitest.config.ts"
    if not vitest_config.is_file():
        fail("Upstream Vitest configuration is missing")
    command = "pnpm.cmd" if os.name == "nt" else "pnpm"
    subprocess.run(
        [command, "exec", "vitest", "run", "--root", str(source), "--config", str(vitest_config), *tests],
        cwd=source,
        check=True,
    )


def archive(source: Path) -> None:
    destination = STATE / "source.tar.gz"
    excluded = {".git", "node_modules", "target", "dist", ".vite", ".cache"}
    def exclude(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if any(part in excluded for part in Path(info.name).parts):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info
    with tarfile.open(destination, "w:gz") as stream:
        stream.add(source, arcname=".", filter=exclude)
    data = read_state()
    data["source_archive_sha256"] = sha256(destination)
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    data["automation_files"] = {str(p.relative_to(ROOT.parent)): sha256(p) for p in files}
    write_json(STATE / "release.json", data)


def unpack(source: Path) -> None:
    source.mkdir(parents=True, exist_ok=True)
    data = read_state()
    archive_path = STATE / "source.tar.gz"
    if sha256(archive_path) != data["source_archive_sha256"]:
        fail("Prepared source archive checksum mismatch")
    with tarfile.open(archive_path, "r:gz") as stream:
        stream.extractall(source, filter="data")


def only(paths: list[Path], label: str) -> Path:
    paths = [p for p in paths if p.is_file()]
    if len(paths) != 1:
        fail(f"Expected exactly one {label}, found {len(paths)}")
    return paths[0]


def collect(source: Path, platform: str, destination: Path) -> None:
    data = read_state()
    destination.mkdir(parents=True, exist_ok=True)
    version = data["upstream_tag"]
    signed = data["signed_updates"]
    target = source / "src-tauri/target"
    base = target / "release"
    if platform == "macos":
        base = target / "universal-apple-darwin/release"
    elif platform == "windows-arm64":
        base = target / "aarch64-pc-windows-msvc/release"
    bundle = base / "bundle"
    assets: list[str] = []

    def copy(path: Path, name: str, updater: bool = False) -> None:
        shutil.copy2(path, destination / name)
        assets.append(name)
        if updater and signed:
            signature = Path(str(path) + ".sig")
            if not signature.is_file() or not signature.read_text(encoding="utf-8").strip():
                fail(f"Missing updater signature: {path.name}")
            shutil.copy2(signature, destination / (name + ".sig"))
            assets.append(name + ".sig")

    if platform.startswith("windows"):
        suffix = "-arm64" if platform.endswith("arm64") else ""
        copy(only(list(bundle.rglob("*.msi")), "MSI"), f"CC-Switch-{version}-Windows{suffix}.msi", True)
        executable = base / "cc-switch.exe"
        if not executable.is_file():
            fail(f"Missing portable executable: {executable}")
        name = f"CC-Switch-{version}-Windows{suffix}-Portable.zip"
        with zipfile.ZipFile(destination / name, "w", compression=zipfile.ZIP_DEFLATED) as stream:
            stream.write(executable, "cc-switch.exe")
            marker = "# CC Switch portable build marker\nportable=true\n"
            if suffix:
                marker += "arch=arm64\n"
            stream.writestr("portable.ini", marker)
        assets.append(name)
    elif platform.startswith("linux"):
        arch = "arm64" if platform.endswith("arm64") else "x86_64"
        for extension in ("AppImage", "deb", "rpm"):
            copy(only(list(bundle.rglob(f"*.{extension}")), extension), f"CC-Switch-{version}-Linux-{arch}.{extension}", True)
    elif platform == "macos":
        apps = list((bundle / "macos").glob("*.app"))
        if len(apps) != 1:
            fail("Expected exactly one universal macOS .app")
        app = apps[0]
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        architectures = subprocess.check_output(["lipo", "-archs", str(app / "Contents/MacOS/cc-switch")], text=True).split()
        if set(architectures) != {"x86_64", "arm64"}:
            fail("macOS bundle must contain both arm64 and x86_64")
        name = f"CC-Switch-{version}-macOS.zip"
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(destination / name)], check=True)
        assets.append(name)
        copy(only(list(bundle.rglob("*.dmg")), "macOS DMG"), f"CC-Switch-{version}-macOS.dmg")
        if signed:
            copy(only(list((bundle / "macos").glob("*.tar.gz")), "macOS updater tarball"), f"CC-Switch-{version}-macOS.tar.gz", True)
    else:
        fail(f"Unsupported platform: {platform}")
    write_json(destination / f"build-{platform}.json", {
        "platform": platform,
        "source_archive_sha256": data["source_archive_sha256"],
        "assets": assets,
        "macos_notarized": os.environ.get("MACOS_NOTARIZED", "false") == "true" if platform == "macos" else None,
    })


def updater_files(data: dict) -> dict[str, str]:
    version = data["upstream_tag"]
    files = {
        "darwin-aarch64": f"CC-Switch-{version}-macOS.tar.gz",
        "darwin-x86_64": f"CC-Switch-{version}-macOS.tar.gz",
        "windows-x86_64": f"CC-Switch-{version}-Windows.msi",
        "windows-aarch64": f"CC-Switch-{version}-Windows-arm64.msi",
    }
    for arch, filename_arch in (("x86_64", "x86_64"), ("aarch64", "arm64")):
        for extension, suffix in (("AppImage", ""), ("deb", "-deb"), ("rpm", "-rpm")):
            files[f"linux-{arch}{suffix}"] = f"CC-Switch-{version}-Linux-{filename_arch}.{extension}"
    return files


def make_manifest(data: dict, directory: Path) -> dict:
    url = f"https://github.com/{data['repository']}/releases/download/{data['release_tag']}"
    platforms = {}
    for platform, name in updater_files(data).items():
        if not (directory / name).is_file():
            fail(f"Missing updater artifact for {platform}")
        signature = (directory / (name + ".sig")).read_text(encoding="utf-8").strip()
        if not signature:
            fail(f"Empty signature for {platform}")
        platforms[platform] = {"signature": signature, "url": f"{url}/{name}"}
    return {
        "version": data["version"],
        "notes": f"CC Switch {data['upstream_tag']} clean build r{data['revision']}",
        "pub_date": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }


def verify_public_release(data: dict, directory: Path | None = None) -> None:
    """Check the GitHub feed and assets; the mirror job checks the custom domain.

    A post-publication failure is explicit, but does not delete or rewrite a
    published release. Later scheduled checks retry this health check without
    compiling or publishing the same version again.
    """
    if not data["signed_updates"]:
        return
    expected_updater_endpoints(data)
    base = f"https://github.com/{data['repository']}/releases/download/{data['release_tag']}"
    endpoint = github_update_endpoint(data["repository"])
    expected_files = updater_files(data)
    local = json.loads((directory / "latest.json").read_text(encoding="utf-8")) if directory else None
    last_error = "not checked"
    for attempt, delay in enumerate(GITHUB_PROPAGATION_DELAYS, 1):
        if delay:
            time.sleep(delay)
        try:
            fixed = public_json(base + "/latest.json")
            latest = public_json(endpoint)
            if fixed != latest or (local is not None and fixed != local):
                raise RuntimeError("GitHub latest feed has not propagated to the published manifest")
            if fixed.get("version") != data["version"] or set(fixed.get("platforms", {})) != set(expected_files):
                raise RuntimeError("Public updater manifest has an unexpected version or platform set")
            visited = set()
            for platform, name in expected_files.items():
                entry = fixed["platforms"][platform]
                url = f"{base}/{name}"
                if entry.get("url") != url or not entry.get("signature"):
                    raise RuntimeError(f"Public updater entry is invalid or points outside this release: {platform}")
                if url not in visited:
                    size = public_asset_size(url)
                    if directory and size is not None and size != (directory / name).stat().st_size:
                        raise RuntimeError(f"Anonymous download size does not match the built artifact: {name}")
                    visited.add(url)
            print(f"Verified anonymous GitHub update feed and {len(visited)} signed artifact downloads: {endpoint}")
            return
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError, KeyError, TypeError) as error:
            last_error = str(error)
            print(f"::warning::Public update health check {attempt}/{len(GITHUB_PROPAGATION_DELAYS)}: {last_error}")
    fail(f"Release {data['release_tag']} is published, but its anonymous GitHub update feed could not be verified after bounded retries: {last_error}. The release was preserved; the next scheduled/manual check will retry without rebuilding.")


def verify_signatures(data: dict, directory: Path, names: set[str]) -> None:
    public_key = STATE / "updater-public.key"
    public_key.write_bytes(base64.b64decode(data["updater_public_key"], validate=True))
    for name in sorted(names):
        if not name.endswith(".sig"):
            continue
        encoded = (directory / name).read_text(encoding="utf-8").strip()
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            fail(f"Malformed updater signature: {name}")
        if not raw.startswith(b"untrusted comment:"):
            fail(f"Not a minisign signature: {name}")
        signature = STATE / "verify.sig"
        signature.write_bytes(raw)
        subprocess.run(["minisign", "-Vm", str(directory / name[:-4]), "-p", str(public_key), "-x", str(signature)], check=True)


def sign_checksums(data: dict, directory: Path) -> None:
    """Authenticate the complete checksum list using the installed updater key."""
    if not data["signed_updates"] or not os.environ.get("TAURI_SIGNING_PRIVATE_KEY", "").strip():
        fail("Signing SHA256SUMS.txt requires TAURI_SIGNING_PRIVATE_KEY; unsigned checksum fallback is forbidden")
    checksums = directory / "SHA256SUMS.txt"
    if not checksums.is_file() or not checksums.stat().st_size:
        fail("Cannot sign a missing or empty SHA256SUMS.txt")
    signature = directory / "SHA256SUMS.txt.sig"
    signature.unlink(missing_ok=True)
    # Tauri reads the signing key and optional password from its supported env
    # variables. Neither secret is interpolated into command arguments or logs.
    signer_env = os.environ.copy()
    signer_env.pop("GH_TOKEN", None)
    signer_env.pop("GITHUB_TOKEN", None)
    command = "npx.cmd" if os.name == "nt" else "npx"
    try:
        subprocess.run(
            [command, "--yes", "--package", TAURI_SIGNER_PACKAGE, "tauri", "signer", "sign",
             "--app-version", data["version"], str(checksums.resolve())],
            env=signer_env, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        fail("Tauri checksum signing failed; check the signing key/password and pinned CLI availability")
    if not signature.is_file() or not signature.stat().st_size:
        fail("Tauri signer did not produce SHA256SUMS.txt.sig")
    # Do not publish merely because the signer returned success: verify against
    # the public key embedded in clients and pre-provisioned on the mirror.
    verify_signatures(data, directory, {"SHA256SUMS.txt.sig"})
    print("Signed and verified SHA256SUMS.txt with this distribution's updater key")


def publish(directory: Path) -> None:
    data = read_state()
    if config().get("require_signed_updates", False) and not data["signed_updates"]:
        fail("This distribution requires signed updater artifacts; refusing an unsigned publication")
    all_assets: set[str] = set()
    notarized = False
    for platform in PLATFORMS:
        path = directory / f"build-{platform}.json"
        if not path.is_file():
            fail(f"Missing platform build manifest: {platform}")
        built = json.loads(path.read_text(encoding="utf-8"))
        if built["platform"] != platform or built["source_archive_sha256"] != data["source_archive_sha256"]:
            fail(f"Build provenance mismatch for {platform}")
        if platform == "macos":
            notarized = built["macos_notarized"]
        for name in built["assets"]:
            if Path(name).name != name or name in all_assets or not (directory / name).is_file():
                fail(f"Invalid, duplicate, or missing asset: {name}")
            all_assets.add(name)
    source_name = f"CC-Switch-{data['upstream_tag']}-Clean-Source.tar.gz"
    if sha256(STATE / "source.tar.gz") != data["source_archive_sha256"]:
        fail("Source archive checksum mismatch before publishing")
    shutil.copy2(STATE / "source.tar.gz", directory / source_name)
    all_assets.add(source_name)
    if data["signed_updates"]:
        if not api(f"/repos/{data['repository']}", anonymous=True, allow_404=True):
            fail("Release repository is no longer publicly readable")
        verify_signatures(data, directory, all_assets)
        check_release_continuity(data)
        write_json(directory / "latest.json", make_manifest(data, directory))
        all_assets.add("latest.json")
    checksums = {name: sha256(directory / name) for name in sorted(all_assets)}
    provenance = dict(data, asset_sha256=checksums, macos_notarized=notarized)
    write_json(directory / "clean-build-provenance.json", provenance)
    all_assets.add("clean-build-provenance.json")
    checksums["clean-build-provenance.json"] = sha256(directory / "clean-build-provenance.json")
    (directory / "SHA256SUMS.txt").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())), encoding="utf-8")
    all_assets.add("SHA256SUMS.txt")
    if data["signed_updates"]:
        sign_checksums(data, directory)
        all_assets.add("SHA256SUMS.txt.sig")

    if data["signed_updates"] and data.get("download_base_url"):
        updater_note = "独立签名自动更新已配置；客户端优先使用自有下载域名，同一仓库的 GitHub 更新源作为备用。域名通道在后续镜像任务校验并上线后生效。"
    else:
        updater_note = "独立签名自动更新已配置；更新仅来自本仓库。" if data["signed_updates"] else "当前为手动下载安装模式：未配置独立更新签名和公开更新源，应用内自动更新暂不可用，也不会退回官方广告版。"
    mac_note = "macOS 已使用自己的 Developer ID 签名并公证。" if notarized else "macOS 使用 ad-hoc 签名，未经过 Apple 公证；首次启动可能需要在系统隐私与安全设置中允许。"
    body = f"""## CC Switch {data['upstream_tag']} · 自有供应商版 r{data['revision']}

基于 [官方 {data['upstream_tag']}]({data['upstream_release_url']})，移除官方内置供应商推荐与广告，仅加入 CodexZH（`api.codexzh.com`）与 CcodeZH（`api.ccodezh.com`）两个自有供应商预设；自定义供应商、网站导入、账户认证和其他应用功能沿用上游。预设不含 API Key，选择后由使用者填写自己的 Key 并保存。

{updater_note}

{mac_note}

应用内部版本保持 **{data['version']}**。同一官方版本的清理修订（r1 → r2）需要手动覆盖安装；后续官方版本可按已配置的更新方式升级。

### 构建来源

- 上游提交：`{data['upstream_sha']}`
- 自动化提交：`{data['automation_sha']}`
- 发布标签：`{data['release_tag']}`
- 完整应用源码：`{source_name}`
- 构建信息与文件校验：`clean-build-provenance.json`、`SHA256SUMS.txt`；签名版另含同一更新公钥可验证的 `SHA256SUMS.txt.sig`

发布标签标记本次自动化配置提交。GitHub 默认的 “Source code” 附件是自动化仓库状态；与安装包对应的完整应用源码请使用上面单独附带的 `Clean-Source.tar.gz`。

下载与你系统匹配的 macOS DMG / ZIP、Windows MSI / Portable ZIP，或 Linux AppImage / DEB / RPM。`.sig`、`latest.json` 和 macOS `.tar.gz` 是签名更新附件，手动安装不需要下载。

这是个人维护的衍生构建，不是上游官方发行包；原项目 MIT 许可证及版权声明保留在源码中。
"""
    notes = STATE / "release-notes.md"
    notes.write_text(body, encoding="utf-8")
    repository, tag = data["repository"], data["release_tag"]
    existing = find_release(repository, tag)
    if existing and not existing["draft"]:
        fail("Refusing to overwrite an already published release")
    if existing:
        if existing.get("target_commitish") != data["automation_sha"]:
            fail("Existing draft targets another automation commit; increment release_revision instead of moving the tag")
    else:
        subprocess.run(["gh", "release", "create", tag, "--repo", repository, "--target", data["automation_sha"], "--draft", "--title", f"CC Switch {data['upstream_tag']} Clean r{data['revision']}", "--notes-file", str(notes)], check=True)
        existing = wait_for_created_draft(repository, tag, data["automation_sha"])
    release_id = existing["id"]
    subprocess.run(["gh", "release", "upload", tag, "--repo", repository, "--clobber", *[str(directory / name) for name in sorted(all_assets)]], check=True)
    uploaded = api(f"/repos/{repository}/releases/{release_id}")
    if not uploaded["draft"] or uploaded["tag_name"] != tag or uploaded.get("target_commitish") != data["automation_sha"]:
        fail("Draft release changed during upload; refusing to publish")
    actual_assets = {asset["name"]: asset["size"] for asset in uploaded["assets"]}
    if set(actual_assets) != all_assets:
        fail("Release draft contains missing or stale attachments; keep it unpublished and use a new clean revision")
    for name in all_assets:
        if actual_assets.get(name) != (directory / name).stat().st_size:
            fail(f"Release upload is incomplete: {name}; draft remains unpublished")
    subprocess.run(["gh", "release", "edit", tag, "--repo", repository, "--notes-file", str(notes), "--draft=false", "--prerelease=false", "--latest"], check=True)
    print(f"Published https://github.com/{repository}/releases/tag/{tag}")
    verify_public_release(data, directory)


def maintain_schedule() -> None:
    """Append a monthly check record so a public repository remains active.

    This uses the Contents API's existing blob SHA as a write lease. It only
    updates one JSON status file; it never resets branches or edits source.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        print("Periodic repository activity is only recorded for scheduled runs")
        return
    repository = os.environ["GITHUB_REPOSITORY"]
    repo = api(f"/repos/{repository}")
    if repo.get("private", True):
        print("Private repositories do not need a public-workflow inactivity record")
        return
    branch = repo["default_branch"]
    path = f"/repos/{repository}/contents/.clean/check-state.json"
    current = api(path + "?ref=" + urllib.parse.quote(branch, safe=""), allow_404=True)
    now = dt.datetime.now(dt.timezone.utc)
    if current:
        content = json.loads(base64.b64decode(current["content"]).decode("utf-8"))
        last = dt.datetime.fromisoformat(content["checked_at"].replace("Z", "+00:00"))
        if last.tzinfo is None or last > now:
            fail("Periodic check timestamp is invalid; refusing to overwrite it")
        if (now - last).total_seconds() < 30 * 24 * 60 * 60:
            print("Periodic check record is less than 30 days old; no commit needed")
            return
    content = {
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "workflow_run": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        "prepare_result": os.environ.get("PREPARE_RESULT", "unknown"),
        "purpose": "Record the periodic upstream check and prevent the 60-day inactivity timeout for public scheduled workflows.",
    }
    encoded = base64.b64encode((json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode("utf-8")).decode("ascii")
    payload = {
        "message": "chore: record monthly upstream check [skip ci]",
        "branch": branch,
        "content": encoded,
    }
    if current:
        payload["sha"] = current["sha"]
    payload_path = STATE / "check-state-update.json"
    write_json(payload_path, payload)
    subprocess.run(["gh", "api", "--method", "PUT", path, "--input", str(payload_path), "--silent"], check=True)
    print("Recorded monthly repository activity without changing application source")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("detect", "configure", "test", "archive", "unpack", "collect", "publish", "maintain-schedule"))
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--assets", type=Path, default=Path("release-assets"))
    args = parser.parse_args()
    if args.command == "detect":
        detect()
    elif args.command == "configure":
        configure(args.source)
    elif args.command == "test":
        test(args.source)
    elif args.command == "archive":
        archive(args.source)
    elif args.command == "unpack":
        unpack(args.source)
    elif args.command == "collect":
        if not args.platform:
            parser.error("collect requires --platform")
        collect(args.source, args.platform, args.assets)
    elif args.command == "publish":
        publish(args.assets)
    elif args.command == "maintain-schedule":
        maintain_schedule()


if __name__ == "__main__":
    main()
