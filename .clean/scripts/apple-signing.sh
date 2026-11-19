#!/usr/bin/env bash
set -euo pipefail

# Secrets arrive through environment variables, never shell interpolation.
configured=0
for name in APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID; do
  if [ -n "${!name:-}" ]; then configured=$((configured + 1)); fi
done
if [ "$configured" -eq 0 ]; then
  echo 'APPLE_SIGNING_IDENTITY=-' >> "$GITHUB_ENV"
  echo 'MACOS_NOTARIZED=false' >> "$GITHUB_ENV"
  echo 'macOS: using ad-hoc signing; no Apple notarization credentials configured.'
  exit 0
fi
if [ "$configured" -ne 5 ]; then
  echo '::error::Partial Apple signing configuration. Set all five Apple secrets or remove all of them.'
  exit 1
fi

keychain_path="$RUNNER_TEMP/cc-switch-clean.keychain-db"
certificate_path="$RUNNER_TEMP/cc-switch-clean.p12"
keychain_password="$(openssl rand -hex 32)"
printf '::add-mask::%s\n' "$keychain_password"
printf '%s' "$APPLE_CERTIFICATE" | base64 --decode > "$certificate_path"
security default-keychain -d user > "$RUNNER_TEMP/cc-switch-original-keychain.txt"
security create-keychain -p "$keychain_password" "$keychain_path"
security set-keychain-settings -lut 21600 "$keychain_path"
security unlock-keychain -p "$keychain_password" "$keychain_path"
security default-keychain -s "$keychain_path"
security list-keychains -d user -s "$keychain_path" "$HOME/Library/Keychains/login.keychain-db"
security import "$certificate_path" -k "$keychain_path" -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain_path"
identity="$(security find-identity -v -p codesigning "$keychain_path" | sed -n 's/.*"\(Developer ID Application:.*\)".*/\1/p' | head -n 1)"
if [ -z "$identity" ]; then
  echo '::error::Apple certificate contains no Developer ID Application signing identity.'
  exit 1
fi
printf 'APPLE_SIGNING_IDENTITY=%s\n' "$identity" >> "$GITHUB_ENV"
echo 'MACOS_NOTARIZED=true' >> "$GITHUB_ENV"
rm -f "$certificate_path"
echo 'macOS: Developer ID configured; the build must pass notarization and verification.'
