#!/usr/bin/env python3
"""Apply the reviewed clean patch to an exact, pristine upstream checkout."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from clean_guard import CLEAN_DIR, RELEASE_LINK_FILES, assert_audited_upstream, git


def replace_exact(text: str, old: str, new: str, count: int, path: str) -> str:
    actual = text.count(old)
    if actual != count:
        raise RuntimeError(f"{path}: expected {count} update-link anchors, found {actual}; review upstream.")
    return text.replace(old, new)


def configure_release_links(source: Path, repository: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("CLEAN_REPOSITORY must be an owner/name GitHub repository.")
    if repository.lower() == "farion1231/cc-switch":
        raise RuntimeError("A clean distribution cannot use the official release channel.")
    official = "https://github.com/farion1231/cc-switch/releases"
    own = f"https://github.com/{repository}/releases"
    for relative in sorted(RELEASE_LINK_FILES):
        path = source / relative
        text = path.read_text(encoding="utf-8")
        if relative.endswith("AboutSection.tsx"):
            text = replace_exact(text, f"`{official}/tag/${{displayVersion}}`", f'"{own}"', 1, relative)
        elif relative.endswith("NewLayoutDialog.tsx"):
            text = replace_exact(
                text,
                "      const version = await getVersion();\n      await settingsApi.openExternal(\n        version ? `${RELEASES_URL}/tag/v${version}` : RELEASES_URL,\n      );",
                "      await settingsApi.openExternal(RELEASES_URL);",
                1,
                relative,
            )
        elif relative.endswith("misc.rs"):
            # Portable download follows the same latest release as the update feed.
            # Keep upstream asset names; downstream tags include the clean patch revision.
            text = replace_exact(
                text,
                f"{official}/download/v{{version}}/CC-Switch-v{{version}}-Windows{{suffix}}-Portable.zip",
                f"{own}/latest/download/CC-Switch-v{{version}}-Windows{{suffix}}-Portable.zip",
                1,
                relative,
            )
            for version, suffix in (("4.0.12", ""), ("4.1.0-beta.1", "-arm64")):
                text = replace_exact(
                    text,
                    f"{official}/download/v{version}/CC-Switch-v{version}-Windows{suffix}-Portable.zip",
                    f"{own}/latest/download/CC-Switch-v{version}-Windows{suffix}-Portable.zip",
                    1,
                    relative,
                )
        if official not in text:
            raise RuntimeError(f"{relative}: upstream release URL anchor disappeared; review required.")
        text = text.replace(official, own)
        path.write_text(text, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: apply-clean.py SOURCE_DIRECTORY")
    source = Path(sys.argv[1]).resolve(strict=True)
    if git(source, "status", "--porcelain", "--untracked-files=normal"):
        raise RuntimeError("Refusing to patch a non-pristine checkout.")
    assert_audited_upstream(source)
    patch = CLEAN_DIR / "patches/clean.patch"
    subprocess.run(["git", "-C", str(source), "apply", "--check", str(patch)], check=True)
    subprocess.run(["git", "-C", str(source), "apply", str(patch)], check=True)
    repository = os.environ.get("CLEAN_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise RuntimeError("Set CLEAN_REPOSITORY to the downstream release repository.")
    configure_release_links(source, repository)
    print(json.dumps({"upstream_commit": git(source, "rev-parse", "HEAD"), "release_repository": repository}))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Clean patch stopped: {exc}", file=sys.stderr)
        sys.exit(1)
