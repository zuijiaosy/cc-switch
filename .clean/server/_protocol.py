"""Wire validation shared in form with the GitHub sync client; no network access."""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

SUMS = "SHA256SUMS.txt"
SUMS_SIG = SUMS + ".sig"
PROVENANCE = "clean-build-provenance.json"
CHUNK = 1024 * 1024

class PublishError(RuntimeError):
    pass

def require(condition: object, message: str) -> None:
    if not condition:
        raise PublishError(message)

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
        raise PublishError("Invalid Tauri/minisign public key") from error


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
        raise PublishError("Invalid base64-encoded minisign signature") from error


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
            raise PublishError(f"Cannot verify {path.name}: minisign is unavailable or timed out") from error
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
        raise PublishError(f"Invalid JSON: {label}") from error
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
        raise PublishError("Checksum list must be ASCII") from error
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


