#!/bin/bash
# Verify a built iSH .app contains everything the unsigned IPA needs.
# Usage: verify_app.sh "/path/to/iSH ARM64.app"
#
# Note: paths contain spaces ("iSH ARM64.app"), so nothing here goes through
# eval/string-splitting - every check quotes its arguments directly.
set -euo pipefail

APP="${1:?usage: verify_app.sh <app bundle>}"
fail=0

ok()   { echo "  OK   $1"; }
bad()  { echo "  FAIL $1"; fail=1; }
check() { if "$@" >/dev/null 2>&1; then ok "$DESC"; else bad "$DESC"; fi; }

echo "Verifying: $APP"

DESC="bundle exists"
check test -d "$APP"

EXE=""
if [ -f "$APP/Info.plist" ]; then
  EXE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Info.plist" 2>/dev/null || true)"
fi

DESC="Info.plist parses (CFBundleExecutable=$EXE)"
check test -n "$EXE"

DESC="executable present: $EXE"
check test -x "$APP/$EXE"

DESC="root.tar.gz bundled"
check test -s "$APP/root.tar.gz"

# The rootfs must actually contain opencode, not just be scheduled to.
if [ -s "$APP/root.tar.gz" ]; then
  LIST="$(tar -tzf "$APP/root.tar.gz")"
  DESC="rootfs has usr/local/bin/opencode.real"
  check grep -qx 'usr/local/bin/opencode.real' <<<"$LIST"
  DESC="rootfs has usr/local/bin/opencode"
  check grep -qx 'usr/local/bin/opencode' <<<"$LIST"
  DESC="rootfs has musl loader"
  check grep -q 'ld-musl-aarch64' <<<"$LIST"
  echo "  root.tar.gz size: $(du -h "$APP/root.tar.gz" | cut -f1)"
fi

# arm64 only: a stray x86_64 slice installs but crashes on launch.
if [ -n "$EXE" ] && [ -f "$APP/$EXE" ]; then
  ARCHS="$(lipo -archs "$APP/$EXE" 2>/dev/null || echo unknown)"
  DESC="binary is arm64 (found: $ARCHS)"
  check grep -qw arm64 <<<"$ARCHS"
fi

if [ "$fail" -ne 0 ]; then
  echo "verification FAILED"
  exit 1
fi
echo "verification passed"
