#!/usr/bin/env python3
"""Check the scope and distribution invariants before any clean binary is built."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from clean_guard import RELEASE_LINK_FILES, assert_allowed_diff
from release import expected_updater_endpoints, public_key_material


def verify_update_configuration(tauri: dict, provenance: dict, repository: str | None = None) -> None:
    if type(provenance.get("signed_updates")) is not bool:
        raise RuntimeError("Clean provenance is missing the signed update mode.")
    if repository and provenance.get("repository") != repository:
        raise RuntimeError("Clean provenance does not belong to this distribution repository.")
    if provenance.get("version") != tauri["version"]:
        raise RuntimeError("Clean provenance and application versions disagree.")
    updater = tauri["plugins"].get("updater", {})
    try:
        expected = expected_updater_endpoints(provenance)
        if updater.get("endpoints", []) != expected:
            raise RuntimeError("Native updater endpoints differ from the approved download channel and GitHub fallback.")
        if provenance["signed_updates"] and public_key_material(updater.get("pubkey", "")) != public_key_material(provenance["updater_public_key"]):
            raise RuntimeError("Native updater public key differs from clean build provenance.")
    except SystemExit as error:
        raise RuntimeError(str(error)) from error
    if tauri["bundle"].get("createUpdaterArtifacts") is not provenance["signed_updates"]:
        raise RuntimeError("Native updater artifact generation differs from the approved signing mode.")


def main() -> None:
    source = Path(sys.argv[1]).resolve(strict=True)
    assert_allowed_diff(source)
    for relative in RELEASE_LINK_FILES:
        text = (source / relative).read_text(encoding="utf-8")
        if "https://github.com/farion1231/cc-switch/releases" in text:
            raise RuntimeError(f"Official binary download URL survived in {relative}.")
    tauri = json.loads((source / "src-tauri/tauri.conf.json").read_text())
    package = json.loads((source / "package.json").read_text())
    if tauri["version"] != package["version"]:
        raise RuntimeError("Package and native application versions disagree.")
    if tauri["plugins"]["deep-link"]["desktop"]["schemes"] != ["ccswitch"]:
        raise RuntimeError("The website import protocol must remain ccswitch://.")
    provenance_path = source / "clean-build-provenance.json"
    if not provenance_path.is_file():
        raise RuntimeError("Clean build provenance is missing; configure the release before verification.")
    verify_update_configuration(
        tauri,
        json.loads(provenance_path.read_text()),
        os.environ.get("CLEAN_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY"),
    )
    database = (source / "src-tauri/src/database/dao/providers.rs").read_text()
    seed = re.search(r"pub fn init_default_official_providers\([^\n]*\{([\s\S]*?)\n    }", database)
    if not seed or "Ok(0)" not in seed.group(1) or "save_provider" in seed.group(1):
        raise RuntimeError("Automatic bundled-provider seeding is still enabled.")
    api_key = (source / "src/components/providers/forms/shared/ApiKeySection.tsx").read_text()
    if re.search(r"t\(`providerForm\.partnerPromotion", api_key):
        raise RuntimeError("An API-key promotion renderer survived.")
    print("Clean scope, no automatic seeds, no promotion renderer, website import, and update-channel checks passed.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, IndexError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"Clean verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
