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
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(".clean-state")
API = "https://api.github.com"
PLATFORMS = ("macos", "windows-x64", "windows-arm64", "linux-x64", "linux-arm64")


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


def output(name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        fail("Refusing a multiline workflow output")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")
    print(f"{name}={value}")


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
    release = api(f"/repos/{upstream}/releases/latest")
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        fail("Only an official stable vMAJOR.MINOR.PATCH release may be built")
    clean_tag = f"clean-{tag}-r{revision}"
    existing = find_release(repository, clean_tag)
    if existing and not existing["draft"]:
        print(f"Already published: {clean_tag}; increment release_revision for a new clean-only build.")
        output("build", "false")
        return

    public_key = os.environ.get("TAURI_PUBLIC_KEY", "").strip()
    endpoint = os.environ.get("CLEAN_UPDATE_ENDPOINT", "").strip()
    has_key = os.environ.get("HAS_SIGNING_KEY", "false").lower() == "true"
    signed = bool(public_key or endpoint or has_key)
    if signed and not (public_key and has_key):
        fail("Partial updater configuration: set TAURI_PUBLIC_KEY variable and TAURI_SIGNING_PRIVATE_KEY secret together")
    expected_endpoint = f"https://github.com/{repository}/releases/latest/download/latest.json"
    if signed:
        public_key = normalize_public_key(public_key)
        endpoint = endpoint or expected_endpoint
        if endpoint != expected_endpoint:
            fail("This workflow publishes to its own GitHub releases; an external feed requires a separately configured publisher")
        public_repo = api(f"/repos/{repository}", anonymous=True, allow_404=True)
        if not public_repo or public_repo.get("private", True):
            fail("Signed native updates require publicly readable release assets. This private repository can publish manual downloads; no visibility is changed automatically.")
        print("Signed updater mode: own public GitHub release feed and own signing key")
    else:
        endpoint = ""
        print("::warning::Manual-download mode: no updater signing key/feed configured; native updater has no endpoints and cannot install the official build.")

    commit = resolve_commit(upstream, tag)
    data = {
        "schema": 1,
        "repository": repository,
        "upstream_repository": upstream,
        "upstream_tag": tag,
        "upstream_sha": commit,
        "upstream_release_url": release["html_url"],
        "version": tag[1:],
        "release_tag": clean_tag,
        "revision": revision,
        "automation_sha": os.environ["GITHUB_SHA"],
        "signed_updates": signed,
        "updater_endpoint": endpoint,
        "updater_public_key": public_key,
    }
    write_json(STATE / "release.json", data)
    output("build", "true")
    output("upstream_sha", commit)
    output("upstream_tag", tag)
    output("release_tag", clean_tag)
    output("signed_updates", str(signed).lower())


def configure(source: Path) -> None:
    data = read_state()
    package = json.loads((source / "package.json").read_text(encoding="utf-8"))
    path = source / "src-tauri/tauri.conf.json"
    tauri = json.loads(path.read_text(encoding="utf-8"))
    if package["version"] != data["version"] or tauri["version"] != data["version"]:
        fail("Upstream release tag and application versions disagree")
    updater = tauri["plugins"]["updater"]
    updater["endpoints"] = [data["updater_endpoint"]] if data["signed_updates"] else []
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


def make_manifest(data: dict, directory: Path) -> dict:
    tag, version = data["release_tag"], data["upstream_tag"]
    url = f"https://github.com/{data['repository']}/releases/download/{tag}"
    files = {
        "darwin-aarch64": f"CC-Switch-{version}-macOS.tar.gz",
        "darwin-x86_64": f"CC-Switch-{version}-macOS.tar.gz",
        "windows-x86_64": f"CC-Switch-{version}-Windows.msi",
        "windows-aarch64": f"CC-Switch-{version}-Windows-arm64.msi",
    }
    for arch, filename_arch in (("x86_64", "x86_64"), ("aarch64", "arm64")):
        for extension, suffix in (("AppImage", ""), ("deb", "-deb"), ("rpm", "-rpm")):
            files[f"linux-{arch}{suffix}"] = f"CC-Switch-{version}-Linux-{filename_arch}.{extension}"
    platforms = {}
    for platform, name in files.items():
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


def publish(directory: Path) -> None:
    data = read_state()
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
        write_json(directory / "latest.json", make_manifest(data, directory))
        all_assets.add("latest.json")
    checksums = {name: sha256(directory / name) for name in sorted(all_assets)}
    provenance = dict(data, asset_sha256=checksums, macos_notarized=notarized)
    write_json(directory / "clean-build-provenance.json", provenance)
    all_assets.add("clean-build-provenance.json")
    checksums["clean-build-provenance.json"] = sha256(directory / "clean-build-provenance.json")
    (directory / "SHA256SUMS.txt").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())), encoding="utf-8")
    all_assets.add("SHA256SUMS.txt")

    updater_note = "独立签名自动更新已配置；更新仅来自本仓库。" if data["signed_updates"] else "当前为手动下载安装模式：未配置独立更新签名和公开更新源，应用内自动更新暂不可用，也不会退回官方广告版。"
    mac_note = "macOS 已使用自己的 Developer ID 签名并公证。" if notarized else "macOS 使用 ad-hoc 签名，未经过 Apple 公证；首次启动可能需要在系统隐私与安全设置中允许。"
    body = f"""## CC Switch {data['upstream_tag']} · 无内置供应商版 r{data['revision']}

基于 [官方 {data['upstream_tag']}]({data['upstream_release_url']})，移除内置供应商推荐与广告；自定义供应商、网站导入、账户认证和其他应用功能沿用上游。

{updater_note}

{mac_note}

应用内部版本保持 **{data['version']}**。同一官方版本的清理修订（r1 → r2）需要手动覆盖安装；后续官方版本可按已配置的更新方式升级。

### 构建来源

- 上游提交：`{data['upstream_sha']}`
- 自动化提交：`{data['automation_sha']}`
- 发布标签：`{data['release_tag']}`
- 完整应用源码：`{source_name}`
- 构建信息与文件校验：`clean-build-provenance.json`、`SHA256SUMS.txt`

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
        existing = find_release(repository, tag)
        if not existing or not existing["draft"] or existing.get("target_commitish") != data["automation_sha"]:
            fail("Created draft is missing or has unexpected provenance; refusing to upload")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("detect", "configure", "test", "archive", "unpack", "collect", "publish"))
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


if __name__ == "__main__":
    main()
