#!/bin/bash
# Fails the image build unless the VM can build for the iOS simulator and holds
# no credentials. build.sh runs it inside the VM with XCODE_VERSION.
set -euo pipefail

die() { echo "verify: $*" >&2; exit 1; }

eval "$(/opt/homebrew/bin/brew shellenv)"

# The base image names the app Xcode_27.app for Xcode 27.0, so trust xcode-select over the folder name.
developer="$(xcode-select -p)"
[[ $developer == /Applications/Xcode*.app/Contents/Developer ]] \
  || die "xcode-select points at $developer, want /Applications/Xcode*.app/Contents/Developer"

have="$(xcodebuild -version | head -1)"
[[ $have == "Xcode $XCODE_VERSION"* ]] || die "xcodebuild reports '$have', want 'Xcode $XCODE_VERSION'"

for t in git claude codex armature; do command -v "$t" >/dev/null || die "$t is not installed"; done

xcrun simctl list runtimes available | grep -q '^iOS ' || die "no available iOS simulator runtime"

udid="$(xcrun simctl list devices available -j | jq -r '
  [.devices | to_entries[] | select(.key | test("SimRuntime\\.iOS")) | .value[]
   | select(.name | startswith("iPhone"))][0].udid // empty')"
[ -n "$udid" ] || die "no available iPhone simulator"

xcrun simctl boot "$udid"
timeout 600 xcrun simctl bootstatus "$udid" -b
xcrun simctl shutdown "$udid"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/Smoke/Sources/Smoke"
cat > "$tmp/Smoke/Package.swift" <<'EOF'
// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Smoke",
    platforms: [.iOS(.v17)],
    products: [.library(name: "Smoke", type: .dynamic, targets: ["Smoke"])],
    targets: [.target(name: "Smoke")]
)
EOF
echo 'public func smoke() -> Int { 1 }' > "$tmp/Smoke/Sources/Smoke/Smoke.swift"
(cd "$tmp/Smoke" && xcodebuild -scheme Smoke -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath "$tmp/dd" -quiet build)
products="$tmp/dd/Build/Products/Debug-iphonesimulator"
[ -f "$products/PackageFrameworks/Smoke.framework/Smoke" ] || [ -f "$products/Smoke.o" ] \
  || die "the smoke package built no product: $(ls -R "$products")"

pattern='AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|aws_secret_access_key"?[[:space:]]*[=:][[:space:]]*"?[A-Za-z0-9/+]{40}|AWS_SECRET_ACCESS_KEY=[A-Za-z0-9/+]{40}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|AWS_BEARER_TOKEN_BEDROCK"?[[:space:]]*[=:][[:space:]]*"?[A-Za-z0-9+/=]{20,}|ABSK[A-Za-z0-9+/=]{20,}'
# Built at run time so this script's own text never matches.
probe="$(mktemp)"
printf 'AKIA%s\n' "$(printf 'X%.0s' $(seq 16))" > "$probe"
printf '"AWS_BEARER_TOKEN_%s": "%s"\n' BEDROCK "$(printf 'Y%.0s' $(seq 24))" >> "$probe"
[ "$(grep -cE "$pattern" "$probe")" -eq 2 ] || die "the credential pattern missed a known key shape"
rm -f "$probe"
rc=0
# sshd keeps per-login agent sockets under ~/.ssh/agent, which grep can't read.
hits="$(sudo grep -rIoHE -D skip "$pattern" "$HOME")" || rc=$?
# AWS documentation sample keys (…EXAMPLE, …EXAMPLEKEY) ship inside SDK gems.
leaks="$(printf '%s\n' "$hits" | grep -v EXAMPLE | cut -d: -f1 | sort -u | grep . || true)"
[ "$rc" -ne 2 ] || die "the credential scan could not read every file"
[ -z "$leaks" ] || die "credential patterns in: $leaks"

if [ -d "$HOME/.ssh" ]; then
  extra="$(find "$HOME/.ssh" -mindepth 1 ! -name authorized_keys ! -name known_hosts ! -type s ! -path "$HOME/.ssh/agent")"
  [ -z "$extra" ] || die "unexpected files in ~/.ssh: $extra"
fi

echo "verify: ok"
