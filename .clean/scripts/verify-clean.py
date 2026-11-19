#!/usr/bin/env python3
"""Check the scope and distribution invariants before any clean binary is built."""

import json
import re
import subprocess
import sys
from pathlib import Path

from clean_guard import RELEASE_LINK_FILES, assert_allowed_diff


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
    for endpoint in tauri["plugins"].get("updater", {}).get("endpoints", []):
        if "farion1231/cc-switch" in endpoint or "dl.ccswitch.io" in endpoint:
            raise RuntimeError("An official updater fallback would restore built-in providers.")
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
