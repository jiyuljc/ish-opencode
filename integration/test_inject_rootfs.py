import unittest
import tarfile
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_rootfs(path: Path, with_loader: bool = True, with_sh: bool = True) -> None:
    """Smallest rootfs that passes/fails the script's bootability checks."""
    with tarfile.open(path, "w:gz") as tar:
        def add(name, data=b"", mode=0o755, type_=tarfile.REGTYPE, link=""):
            ti = tarfile.TarInfo(name)
            ti.mode = mode
            ti.type = type_
            ti.linkname = link
            ti.size = len(data) if type_ == tarfile.REGTYPE else 0
            tar.addfile(ti, io.BytesIO(data) if type_ == tarfile.REGTYPE else None)

        add("./", type_=tarfile.DIRTYPE, mode=0o755)
        add("./bin", type_=tarfile.DIRTYPE, mode=0o755)
        add("./etc", type_=tarfile.DIRTYPE, mode=0o755)
        add("./lib", type_=tarfile.DIRTYPE, mode=0o755)
        if with_sh:
            add("./bin/sh", b"\x7fELF")
        add("./etc/apk/repositories", b"https://x\n", 0o644)
        if with_loader:
            add("./lib/ld-musl-aarch64.so.1", b"\x7fELF")
            add("./lib/libc.musl-aarch64.so.1", type_=tarfile.SYMTYPE,
                link="ld-musl-aarch64.so.1")
            add("./usr", type_=tarfile.DIRTYPE)
            add("./usr/bin", type_=tarfile.DIRTYPE)
            add("./usr/bin/busybox", b"\x7fELF")
            # a hardlink, which a naive append-only rewrite would corrupt
            add("./bin/cat", type_=tarfile.LNKTYPE, link="usr/bin/busybox")


class InjectTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "opencode"
        # script rejects < 50 MB as a wrong asset, so pad rather than assert
        self.bin.write_bytes(b"\x7fELF" + b"\0" * (55 * 1024 * 1024))

    def run_script(self, rootfs: Path, *extra):
        import subprocess
        return subprocess.run(
            [sys.executable, str(Path(__file__).parent / "inject_rootfs.py"),
             "--rootfs", str(rootfs), "--opencode", str(self.bin),
             "--out", str(self.tmp / "out.tar.gz"), *extra],
            capture_output=True, text=True)

    def test_injects_and_preserves_every_entry(self):
        src = self.tmp / "root.tar.gz"
        build_rootfs(src)
        r = self.run_script(src)
        self.assertEqual(r.returncode, 0, r.stderr)
        with tarfile.open(src) as a, tarfile.open(self.tmp / "out.tar.gz") as b:
            def names(t):
                # script normalises "./x" -> "x"; the archive root "./" is
                # dropped entirely (no need to recreate the destination dir)
                return {("." if m.name in (".", "./") else m.name.removeprefix("./"))
                        for m in t} - {"."}
            before, after = names(a), names(b)
            self.assertLessEqual(before, after, "entries were lost")
            self.assertTrue({"usr/local/bin/opencode",
                             "usr/local/bin/opencode.real"} <= after)
            # hardlink must survive the rewrite as a hardlink (names normalised)
            self.assertEqual(b.getmember("bin/cat").type, tarfile.LNKTYPE)
            real = b.extractfile("usr/local/bin/opencode.real").read()
            self.assertEqual(len(real), len(self.bin.read_bytes()))
            wrapper = b.extractfile("usr/local/bin/opencode").read().decode()
            self.assertIn("exec /usr/local/bin/opencode.real", wrapper)
            self.assertIn("OPENCODE_DISABLE_JIT=1", wrapper)
            self.assertEqual(b.getmember("usr/local/bin/opencode.real").mode & 0o111,
                             0o111)

    def test_refuses_rootfs_without_shell(self):
        src = self.tmp / "root.tar.gz"
        build_rootfs(src, with_sh=False)
        r = self.run_script(src)
        self.assertEqual(r.returncode, 3)
        self.assertIn("bin/sh", r.stderr)

    def test_refuses_rootfs_without_musl_loader(self):
        src = self.tmp / "root.tar.gz"
        build_rootfs(src, with_loader=False)
        r = self.run_script(src)
        self.assertEqual(r.returncode, 3)
        self.assertIn("ld-musl", r.stderr)

    def test_refuses_wrong_asset_size(self):
        self.bin.write_bytes(b"\x7fELF" + b"\0" * 1024)
        src = self.tmp / "root.tar.gz"
        build_rootfs(src)
        r = self.run_script(src)
        self.assertEqual(r.returncode, 1)
        self.assertIn("too small", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
