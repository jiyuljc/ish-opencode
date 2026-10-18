#!/bin/bash
# Verify a built iSH .app contains everything the unsigned IPA needs.
# Usage: verify_app.sh "/path/to/iSH ARM64.app"
set -euo pipefail

APP="${1:?usage: verify_app.sh <app bundle>}"
fail=0

check() { # name, test-command
  if eval "$2"; then
    echo "  OK   $1"
  else
    echo "  FAIL $1"
    fail=1
  fi
}

echo "Verifying: $APP"
check "bundle exists"            "[[ -d '$APP' ]]"
check "executable present"       "[[ -x '$APP/\$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' '$APP/Info.plist')' ]]"
check "Info.plist parses"        "/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' '$APP/Info.plist' >/dev/null"
check "root.tar.gz bundled"      "[[ -s '$APP/root.tar.gz' ]]"

# The rootfs must actually contain opencode, not just be scheduled to.
if [[ -s "$APP/root.tar.gz" ]]; then
  LIST=$(tar -tzf "$APP/root.tar.gz")
  check "rootfs has usr/local/bin/opencode.real" "grep -qx 'usr/local/bin/opencode.real' <<<\"\$LIST\""
  check "rootfs has usr/local/bin/opencode"      "grep -qx 'usr/local/bin/opencode' <<<\"\$LIST\""
  check "rootfs has musl loader"                 "grep -q 'ld-musl-aarch64' <<<\"\$LIST\""
  echo "  root.tar.gz size: $(du -h "$APP/root.tar.gz" | cut -f1)"
fi

# arm64 only: a stray x86_64 slice installs but crashes on launch.
BIN=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Info.plist")
check "binary is arm64" "lipo -archs '$APP/$BIN' 2>/dev/null | grep -qw arm64"

if [[ $fail -ne 0 ]]; then
  echo "verification FAILED"
  exit 1
fi
echo "verification passed"
