#!/usr/bin/env python3
"""Inject opencode into an iSH-ARM64 rootfs tarball.

iSH-ARM64's ARM64 target downloads `root.tar.gz` at build time (AppARM64 ->
GuestARM64.xcconfig: ROOTFS_URL = Alpine minirootfs) and imports it into a
fakefs on first launch. The tarball *is* the user's root filesystem, so
pre-loading a file means rewriting the tarball - not the Xcode project.

Pure Python on purpose: no GNU tar on Windows and no bsdtar requirement, so the
same script runs on the macOS runner and on a dev box for verification.

Two things here are load-bearing and non-obvious:

1. opencode's PT_INTERP is /lib/ld-musl-aarch64.so.1. The loader is resolved
   before opencode runs, so a wrapper script cannot help it - the binary must
   live on the same filesystem as /lib, hence /usr/local/bin (not /mnt, which
   iSH bind-mounts over an iOS container directory).
2. The Bun runtime inside opencode is injected with --jitless by iSH, so the
   wrapper must also disable runtime codegen (OPENCODE_DISABLE_JIT).

Exit codes: 0 ok, 3 = rootfs lacks something needed to boot.
"""

from __future__ import annotations

import argparse
import io
import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

INSTALL_DIR = "usr/local/bin"
REAL_NAME = "opencode.real"
WRAPPER_NAME = "opencode"
MARKER = "etc/ish-opencode-build"

WRAPPER = """#!/bin/sh
# Launched by iSH's emulated Bun. Disable runtime codegen before exec: the
# loader resolves PT_INTERP first, so this cannot live inside the binary.
OPENCODE_DISABLE_JIT=1
export OPENCODE_DISABLE_JIT
exec /usr/local/bin/%s "$@"
""" % REAL_NAME

# Entries that must already exist, else the guest will not boot and we would
# otherwise ship a "successful" IPA that hangs on first launch.
REQUIRED_PATHS = ("bin/sh", "etc/apk/repositories")
MUSL_LOADER_PREFIX = "lib/ld-musl-"


def log(msg: str) -> None:
    print(f"[inject] {msg}", flush=True)


def fail(msg: str, code: int = 1) -> "None":
    print(f"[inject] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def normalise(name: str) -> str:
    """'./usr/bin/x' -> 'usr/bin/x'; drops leading './' and slashes."""
    n = posixpath.normpath(name.lstrip("./"))
    return "" if n == "." else n


class Rootfs:
    """Minimal writable view of a rootfs tarball.

    Streams the source once. Only paths we insert are buffered in memory; we are
    not root, so device nodes from the Alpine image are recorded but not created.
    """

    def __init__(self, src: Path) -> None:
        self.src = src
        self.entries: list[tarfile.TarInfo] = []
        self.contents: dict[str, bytes] = {}   # name -> data, for regular files
        self.links: dict[str, str] = {}        # name -> symlink target
        self.special: list[tarfile.TarInfo] = []  # device/fifo, copied as-is
        self.names: set[str] = set()

    def load(self) -> None:
        log(f"reading {self.src.name} ({self.src.stat().st_size/1e6:.1f} MB)")
        with tarfile.open(self.src, "r:gz") as tar:
            for ti in tar:
                name = normalise(ti.name)
                if not name:
                    continue  # root entry "./"
                ti.name = name
                self.names.add(name)
                self.entries.append(ti)
                if ti.isreg():
                    fh = tar.extractfile(ti)
                    self.contents[name] = fh.read() if fh else b""
                elif ti.issym() or ti.islnk():
                    self.links[name] = ti.linkname
                elif not (ti.isdir()):
                    self.special.append(ti)

    def has(self, name: str) -> bool:
        return normalise(name) in self.names

    def loader_names(self) -> list[str]:
        return sorted(n for n in self.names if n.startswith(MUSL_LOADER_PREFIX))

    def add_file(self, name: str, data: bytes, mode: int = 0o755) -> None:
        name = normalise(name)
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        ti.mode = mode
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        ti.mtime = 1_700_000_000
        ti.type = tarfile.REGTYPE
        self.entries = [e for e in self.entries if normalise(e.name) != name]
        self.entries.append(ti)
        self.contents[name] = data
        self.names.add(name)

    def write(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        # Deterministic order: directories first so early extraction never hits
        # a missing parent, then the rest sorted.
        with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT,
                          compresslevel=6) as tar:
            for ti in self.entries:
                if ti.isreg():
                    data = self.contents.get(normalise(ti.name), b"")
                    ti.size = len(data)
                    tar.addfile(ti, io.BytesIO(data))
                else:
                    tar.addfile(ti)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rootfs", required=True, type=Path, help="input root.tar.gz")
    ap.add_argument("--out", required=True, type=Path, help="output root.tar.gz")
    ap.add_argument("--opencode", required=True, type=Path, help="linux-arm64-musl binary")
    ap.add_argument("--keep-extracted", type=Path, default=None,
                    help="also unpack a copy here for local inspection")
    args = ap.parse_args()

    if not args.opencode.is_file():
        fail(f"opencode binary not found: {args.opencode}")
    size = args.opencode.stat().st_size
    if size < 50 * 1024 * 1024:
        fail(f"opencode looks too small ({size} bytes); wrong/mislabelled asset?")

    fs = Rootfs(args.rootfs)
    fs.load()

    missing = [p for p in REQUIRED_PATHS if not fs.has(p)]
    if missing:
        fail(f"rootfs is missing {missing}; refusing to produce an unbootable image", 3)
    loaders = fs.loader_names()
    if not loaders:
        fail("rootfs has no lib/ld-musl-* loader; is this an Alpine aarch64 image?", 3)
    log(f"musl loader present: {', '.join(loaders)}")

    log(f"installing {INSTALL_DIR}/{REAL_NAME} ({size/1e6:.1f} MB)")
    fs.add_file(f"{INSTALL_DIR}/{REAL_NAME}", args.opencode.read_bytes(), 0o755)
    fs.add_file(f"{INSTALL_DIR}/{WRAPPER_NAME}", WRAPPER.encode(), 0o755)
    fs.add_file(MARKER, f"opencode.real={size}\nsource=ish-opencode CI\n".encode(), 0o644)

    log(f"writing {args.out}")
    fs.write(args.out)
    log(f"done: {args.out} ({args.out.stat().st_size/1e6:.1f} MB, "
        f"{len(fs.entries)} entries)")

    if args.keep_extracted:
        log(f"extracting copy for inspection -> {args.keep_extracted}")
        # Debug convenience only. Extraction semantics differ across Python
        # versions (set_attrs, 3.14 filters), and none of it affects the
        # tarball we just wrote, so never let it fail the run.
        try:
            with tarfile.open(args.out, "r:gz") as tar:
                for member in tar:
                    if not (member.isreg() or member.isdir() or member.issym()):
                        continue  # skip device nodes on unprivileged hosts
                    try:
                        tar.extract(member, args.keep_extracted)
                    except Exception:  # noqa: BLE001 - best effort
                        pass
        except Exception as exc:  # noqa: BLE001
            log(f"(inspection extract skipped: {exc})")


if __name__ == "__main__":
    main()
