"""End-to-end test of the Download Root rewrite against the real pbxproj.

Not run in CI (CI applies the patches to the live checkout and its post-
conditions catch breakage). This exists because the replacement is text
manipulation on a 200 KB pbxproj, and a silent no-op would ship an IPA whose
rootfs came from a URL that no longer works.

Run from the repo root:  python integration/test_pbxproj_rewrite.py

Escaping note (this cost several debugging cycles):
  the pbxproj stores the script newline as LITERAL backslash + 'n', so the
  bytes at the end of the phase are:   dot g z backslash quote backslash n
  quote semicolon.
  In a single-quoted Python literal, the tail is written with ONE backslash
  before the quote and ONE before 'n'. Using two backslashes produces
  backslash-backslash-n on disk and the phase never matches.
  Read the file via `git show` into Python, never through a shell redirect.
  Also: re.subn treats the replacement as a template and processes its
  backslashes - apply_patches.py uses a lambda to keep the bytes verbatim.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_patches  # noqa: E402

root = Path(__file__).resolve().parents[1]
work = root / "build" / "regextest"
work.mkdir(parents=True, exist_ok=True)
pbx = work / "project.pbxproj"

raw = subprocess.run(
    ["git", "-C", str(root), "show",
     "upstream/master:iSH.xcodeproj/project.pbxproj"],
    capture_output=True, check=True).stdout
pbx.write_bytes(raw)
before = raw.decode("utf-8")

# Same literals the production code uses.
old = ('shellScript = "curl -L \\"https://$ROOTFS_URL\\" -o '
       '\\"$BUILT_PRODUCTS_DIR/$CONTENTS_FOLDER_PATH/root.tar.gz\\"\\n";')
new = ('shellScript = "cp \\"$SRCROOT/../build/rootfs/root.tar.gz\\" '
       '\\"$BUILT_PRODUCTS_DIR/$CONTENTS_FOLDER_PATH/root.tar.gz\\"\\n";')

n_old = before.count(old)
print(f"upstream Download Root phases: {n_old}")
assert n_old == 3, f"expected 3 phases, found {n_old}"

expected = before.replace(old, new)

first = apply_patches.patch_pbxproj(pbx)
after_first = pbx.read_text(encoding="utf-8")
second = apply_patches.patch_pbxproj(pbx)   # idempotency
after = pbx.read_text(encoding="utf-8")

print(f"first call  -> {first}   (expect True)")
print(f"second call -> {second}  (expect False: already patched)")
print("curl phases left:", after.count('shellScript = "curl -L'))
print("cp phases now   :", after.count(new))

ok = (
    first is True
    and second is False
    and after.count('shellScript = "curl -L') == 0
    and after.count(new) == n_old
    and after_first == expected     # nothing else in the 200 KB file changed
    and after == expected
)

# Negative test: a pbxproj with no matching phase must fail loudly.
pbx2 = work / "nomatch.pbxproj"
pbx2.write_text("no phases here\n", encoding="utf-8")
none_result = apply_patches.patch_pbxproj(pbx2)
print(f"missing-anchor call -> {none_result}  (expect None)")
ok = ok and none_result is None

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
