#!/usr/bin/env python3
"""Apply the two iSH-ARM64 fixes for an AltStore/SideStore-installable build.

    python3 integration/apply_patches.py --ish-src ish

Why a script and not a .patch + `git apply`: verified by hand that
`git apply` is whitespace/intent-to-add sensitive here and emits
"corrupt patch". This edits by exact code anchors instead, and *fails loudly*
if an anchor is missing - the failure mode we care about is a silent no-op
after upstream refactors, not a whitespace mismatch.

Both fixes are idempotent: re-running says "already patched".

Fix 1 - kernel/uname.c: hostname overflow kills the HOST process.
    struct utsname real_uname;  uname(&real_uname);  strcpy(uts->hostname, ...)
  struct uname.hostname is UNAME_LENGTH (65). A host nodename >= 65 chars, or
  a failed uname() leaving stack garbage, overruns it; fortified libc turns
  that into SIGTRAP and the emulator dies on the guest's first uname() call.

Fix 2 - app/hook.c: SIGKILL in any re-signed build.
  hook() calls thread_set_state(ARM_DEBUG_STATE64) on threads it does not own,
  which needs task_for_pid-allow. An AltStore signature carries no
  entitlements, so iOS terminates the app. It is reachable at startup via the
  accessibilityfixes_init() constructor whenever VoiceOver is on.

Exit: 0 ok/already-patched, 1 anchor missing (upstream changed - review), 2 usage.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def log(m: str) -> None:
    print(f"[patch] {m}", flush=True)


def patch_uname(path: Path) -> bool:
    """Returns True if a change was made, False if already patched."""
    src = path.read_text(encoding="utf-8")
    if "memset(&real_uname, 0, sizeof(real_uname))" in src:
        log(f"{path.name}: already patched")
        return False

    old_decl = """    struct utsname real_uname;
    uname(&real_uname);
"""
    new_decl = """    struct utsname real_uname;
    /* Uninitialised was a latent bug: on failure this is stack garbage with
     * no NUL terminator. */
    memset(&real_uname, 0, sizeof(real_uname));
    if (uname(&real_uname) != 0)
        strcpy(real_uname.nodename, "ish");
"""
    old_copy = "    strcpy(uts->hostname, hostname);\n"
    new_copy = (
        "    /* uts->hostname is UNAME_LENGTH (65) bytes; the host nodename is\n"
        "     * not bounded by it. Linux truncates instead of overflowing. */\n"
        '    snprintf(uts->hostname, sizeof(uts->hostname), "%s", hostname);\n'
    )

    for label, old in (("real_uname decl", old_decl), ("hostname copy", old_copy)):
        if old not in src:
            print(f"[patch] ERROR: anchor '{label}' not found in {path}.\n"
                  f"[patch]        Upstream changed; review before shipping.",
                  file=sys.stderr)
            return None  # type: ignore[return-value]

    src = src.replace(old_decl, new_decl, 1).replace(old_copy, new_copy, 1)
    path.write_text(src, encoding="utf-8")
    log(f"{path.name}: patched")
    return True


def patch_hook(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if "feature disabled" in src:
        log(f"{path.name}: already patched")
        return False

    old = """    task_threads(mach_task_self(), &threads, &thread_count);
    for (int i = 0; i < thread_count; ++i) {
        if (thread_set_state(threads[i], ARM_DEBUG_STATE64, (thread_state_t)&state, ARM_DEBUG_STATE64_COUNT) != KERN_SUCCESS) {
            NSLog(CFSTR("hook failed: could not set thread 0x%x debug state"), threads[i]);
            success = false;
            goto done;
        }
    }
"""
    new = """    task_threads(mach_task_self(), &threads, &thread_count);
    /* Setting debug state on a thread the caller does not own needs
     * task_for_pid-allow. A re-signed build (AltStore/SideStore, free Apple ID)
     * carries no entitlements, so iOS kills the process here - and
     * accessibilityfixes_init() reaches hook() at startup with VoiceOver on.
     * Treat it as unsupported rather than fatal. */
    for (int i = 0; i < thread_count; ++i) {
        if (thread_set_state(threads[i], ARM_DEBUG_STATE64, (thread_state_t)&state, ARM_DEBUG_STATE64_COUNT) != KERN_SUCCESS) {
            NSLog(CFSTR("hook: thread 0x%x debug state unavailable (needs task_for_pid-allow); feature disabled"), threads[i]);
            success = false;
            break;
        }
    }
"""
    if old not in src:
        print(f"[patch] ERROR: thread_set_state loop anchor not found in {path}.\n"
              f"[patch]        Upstream changed; review before shipping.",
              file=sys.stderr)
        return None  # type: ignore[return-value]

    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    log(f"{path.name}: patched")
    return True


def patch_pbxproj(path: Path) -> bool:
    """Replace the Download Root phases' `curl https://$ROOTFS_URL` with a copy.

    The phase text is:
        shellScript = "curl -L \"https://$ROOTFS_URL\" -o \".../root.tar.gz\"\n";
    The scheme is hardcoded, so ROOTFS_URL only supplies a hostname - neither an
    xcconfig edit nor a command-line override can make it read a local file.
    Upstream's own value is also dead (github.com/ish-app/roots no longer
    negotiates TLS 1.0/1.1, and the phase uses the system's old curl), so the
    only working approach is to copy the rootfs we built.
    """
    src = path.read_text(encoding="utf-8")
    pat = re.compile(r'shellScript = "curl -L \\"https://\$ROOTFS_URL\\"[^;]*;')
    # Ground truth from the upstream pbxproj, by code point:
    #   ... root.tar.gz \ " \ n " ;
    # i.e. an escaped quote, then a LITERAL backslash + 'n' (sbproj escaping).
    # In a single-quoted Python literal that is '\\n' as the last four chars:
    # '\\"' =>  \ "     and     '\\n' =>  \ n
    new = 'shellScript = "cp \\"$SRCROOT/../build/rootfs/root.tar.gz\\" ' \
          '\\"$BUILT_PRODUCTS_DIR/$CONTENTS_FOLDER_PATH/root.tar.gz\\"\\n";'
    if new in src:
        log(f"{path.name}: Download Root already copies the local rootfs")
        return False
    # re.subn treats the replacement as a template: backslashes in it are
    # escape-processed, which turned the literal '\n' into a newline and made
    # the idempotency check fail. A lambda replacement is taken verbatim.
    new_src, n = pat.subn(lambda _m: new, src)
    if n == 0:
        print(f"[patch] ERROR: no Download Root curl phase found in {path}.\n"
              f"[patch]        Upstream changed; review before shipping.",
              file=sys.stderr)
        return None  # type: ignore[return-value]
    path.write_text(new_src, encoding="utf-8")
    log(f"{path.name}: rewrote {n} Download Root phase(s)")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ish-src", required=True, type=Path,
                    help="path to the ish-arm64 checkout")
    args = ap.parse_args()

    uname = args.ish_src / "kernel" / "uname.c"
    hook = args.ish_src / "app" / "hook.c"
    pbx = args.ish_src / "iSH.xcodeproj" / "project.pbxproj"
    for f in (uname, hook, pbx):
        if not f.is_file():
            print(f"[patch] ERROR: {f} not found", file=sys.stderr)
            sys.exit(2)

    results = [patch_uname(uname), patch_hook(hook), patch_pbxproj(pbx)]
    if any(r is None for r in results):
        sys.exit(1)

    # Post-conditions: make sure the tree is actually in the expected state,
    # so a partial apply can never reach the compiler.
    u, h = uname.read_text(encoding="utf-8"), hook.read_text(encoding="utf-8")
    p = pbx.read_text(encoding="utf-8")
    checks = {
        "uname.c: real_uname zeroed": "memset(&real_uname, 0" in u,
        "uname.c: uname() result checked": "if (uname(&real_uname) != 0)" in u,
        "uname.c: hostname bounded": "snprintf(uts->hostname" in u,
        "uname.c: no unbounded hostname copy": "strcpy(uts->hostname" not in u,
        "hook.c: non-fatal thread_set_state": "feature disabled" in h,
        "hook.c: old fatal branch gone": "could not set thread" not in h,
        "pbxproj: no Download Root curl left": 'shellScript = "curl -L' not in p,
        "pbxproj: Download Root copies local rootfs":
            p.count("$SRCROOT/../build/rootfs/root.tar.gz") >= 1,
    }
    bad = [k for k, ok in checks.items() if not ok]
    for k, ok in checks.items():
        print(f"  {'OK  ' if ok else 'FAIL'} {k}")
    if bad:
        print("[patch] ERROR: post-conditions failed", file=sys.stderr)
        sys.exit(1)
    log("all patches verified")


if __name__ == "__main__":
    main()
