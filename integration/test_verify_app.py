"""Static checks on verify_app.sh.

No bash available locally, so instead of running it we assert the properties
that caused the CI failure: no eval, and every path-bearing expansion quoted.
The failure was:
    eval "[[ -x '$APP/$(... PlistBuddy ...)' ]]"
which word-splits "iSH ARM64.app" and produced a syntax error.
"""
import re
import sys
from pathlib import Path

p = Path(__file__).resolve().parent / "verify_app.sh"
src = p.read_text(encoding="utf-8")
body = "\n".join(line for line in src.splitlines()
                 if not line.lstrip().startswith("#"))

checks = {
    "no eval": "eval" not in body,
    "no $APP/$EXE concatenation inside single quotes": "'$APP/$EXE'" not in body,
    "quotes $APP in tests": body.count('"$APP"') >= 1,
    "quotes $APP/$EXE": '"$APP/$EXE"' in body,
    "uses here-string for grep (no pipe into grep -q)": "<<<" in body,
    "set -euo pipefail": "set -euo pipefail" in body,
    "has arm64 check": "lipo -archs" in body,
    "has opencode.real check": "usr/local/bin/opencode.real" in body,
}

# Every use of a variable inside [[ ]] or test must be quoted.
for m in re.finditer(r"test -[a-z] ([^\n]+)", body):
    arg = m.group(1).strip()
    if arg.startswith("-") or arg.startswith('"'):
        continue
    if "$" in arg:
        checks[f"unquoted var in: test {arg}"] = False

for k, v in checks.items():
    print(f"  {'OK  ' if v else 'FAIL'} {k}")
ok = all(checks.values())
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
