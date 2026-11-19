#!/usr/bin/env python3
"""Fail closed when upstream grows a provider/promotion entry point we did not audit."""

import json
import re
import subprocess
from pathlib import Path

CLEAN_DIR = Path(__file__).resolve().parents[1]


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *args], text=True, encoding="utf-8"
    ).strip()


def source_inventory(source: Path) -> dict:
    types = (source / "src/lib/api/types.ts").read_text(encoding="utf-8")
    match = re.search(r"export type AppId\s*=([\s\S]*?);", types)
    if not match:
        raise RuntimeError("Upstream AppId declaration changed; review the clean patch.")
    surface = re.compile(
        r"ProviderPresetSelector|universalProviderPresets|partnerPromotionKey|"
        r"\bisPartner\b|SponsorBanner|(?i:sponsored|advertisement|affiliate)"
    )
    surface_counts = {}
    for directory in ("src/components", "src/hooks", "src/lib"):
        for path in sorted((source / directory).rglob("*")):
            if path.suffix not in (".ts", ".tsx") or ".test." in path.name:
                continue
            count = len(surface.findall(path.read_text(encoding="utf-8")))
            if count:
                surface_counts[path.relative_to(source).as_posix()] = count
    seed_calls = {}
    for path in sorted((source / "src-tauri/src").rglob("*.rs")):
        text = path.read_text(encoding="utf-8")
        count = len(re.findall(
            r"(?:init_default_official_providers|ensure_official_seed_by_id)\s*\(", text
        ))
        if count:
            seed_calls[path.relative_to(source).as_posix()] = count
    return {
        "app_ids": sorted(re.findall(r'"([a-z-]+)"', match.group(1))),
        "preset_files": sorted(
            p.relative_to(source).as_posix()
            for p in (source / "src/config").glob("*ProviderPresets.ts")
        ),
        "provider_forms": sorted(
            p.relative_to(source).as_posix()
            for p in (source / "src/components/providers/forms").glob("*ProviderForm.tsx")
        ),
        "presentation_surfaces": surface_counts,
        "seed_call_sites": seed_calls,
    }


def assert_audited_upstream(source: Path) -> None:
    expected = json.loads((CLEAN_DIR / "upstream-guard.json").read_text())
    actual = source_inventory(source)
    differences = [key for key in actual if actual[key] != expected[key]]
    if differences:
        raise RuntimeError(
            "Upstream provider/advertising structure changed: "
            + ", ".join(differences)
            + ". No release will be published until this change is reviewed."
        )


def patch_paths() -> set[str]:
    patch = (CLEAN_DIR / "patches/clean.patch").read_text(encoding="utf-8")
    return set(re.findall(r"^\+\+\+ b/(.+)$", patch, re.MULTILINE))


RELEASE_LINK_FILES = {
    "src/components/DatabaseUpgrade.tsx",
    "src/components/shell/NewLayoutDialog.tsx",
    "src/components/settings/AboutSection.tsx",
    "src-tauri/src/commands/misc.rs",
}


def assert_allowed_diff(source: Path) -> None:
    allowed = patch_paths() | RELEASE_LINK_FILES | {
        "src-tauri/tauri.conf.json", "clean-build-provenance.json"
    }
    changed = set(git(source, "diff", "--name-only", "HEAD").splitlines())
    changed |= set(git(source, "ls-files", "--others", "--exclude-standard").splitlines())
    unexpected = changed - allowed
    if unexpected:
        raise RuntimeError("Changes outside clean distribution scope: " + ", ".join(sorted(unexpected)))
    missing = patch_paths() - changed
    if missing:
        raise RuntimeError("Clean patch was not fully applied: " + ", ".join(sorted(missing)))
