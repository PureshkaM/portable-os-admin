"""Building a medium for another architecture under user-mode emulation.

Filling a root of a foreign architecture is only half the job. Every chroot
step afterwards -- installing the kernel, building the initramfs, installing
the bootloader -- runs *target* binaries, and the kernel hands those to an
emulator through `binfmt_misc`. With `qemu-user-static` registered, all of it
works: measured on an arm64 host against an amd64 Debian image, debootstrap's
second stage, `apt-get install` of the kernel and `grub-efi-amd64`,
`update-initramfs`, `grub-install --target=x86_64-efi` and `update-grub` each
returned 0 and produced a genuine `PE32+ executable for EFI, x86-64`.

So this module does not decide *whether* foreign architectures are allowed --
they are. It decides whether this particular machine is set up for it, and
refuses **before the medium is erased** rather than dying with "Exec format
error" forty minutes into a bootstrap.

Two things are deliberately not hardcoded: which architectures exist (any
`binfmt_misc` registration is honoured, not a fixed list), and which
architecture the target is (it comes from the image and the configuration).
The one table here maps different spellings of the same architecture onto the
kernel's own spelling, which is what `binfmt_misc` and `uname -m` use.
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass

#: Where the kernel exposes its registered interpreters.
BINFMT_DIR = "/proc/sys/fs/binfmt_misc"

#: Distribution package that provides the emulators plus their registration.
#: The same name is used by Debian and by the RHEL family (via EPEL), which is
#: why it is a single string rather than a per-family table.
EMULATOR_PACKAGE = "qemu-user-static"

#: Different families spell the same architecture differently: Debian says
#: `amd64`/`arm64`, RPM and the kernel say `x86_64`/`aarch64`. `binfmt_misc`
#: registrations and `uname -m` both use the kernel spelling, so everything
#: here is normalised to it. Only genuine synonyms belong in this table --
#: it is a dictionary, not a list of supported targets.
KERNEL_ARCH = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "armhf": "arm",
    "armv7l": "arm",
    "arm": "arm",
    "i386": "i386",
    "i686": "i386",
    "ppc64el": "ppc64le",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
    "riscv64": "riscv64",
    "loongarch64": "loongarch64",
}


def kernel_arch(name: str) -> str:
    """Spelling of `name` as the kernel and `binfmt_misc` use it.

    Unknown names are passed through unchanged rather than rejected: a new
    architecture should degrade into "no registration found", which is a
    truthful and actionable message, not into "unsupported by this program".
    """
    return KERNEL_ARCH.get((name or "").strip().lower(), (name or "").strip().lower())


def host_kernel_arch() -> str:
    return kernel_arch(platform.machine())


def is_native(target_arch: str, host_arch_name: str = "") -> bool:
    host = kernel_arch(host_arch_name) if host_arch_name else host_kernel_arch()
    return kernel_arch(target_arch) == host


@dataclass(frozen=True)
class Emulation:
    """What this machine can do about `target_arch`."""

    target_arch: str
    native: bool
    available: bool
    #: path to the emulator binary, as the kernel will invoke it
    interpreter: str = ""
    #: registered with the `F` flag, meaning the kernel opened the emulator at
    #: registration time and keeps it available inside a chroot. Without it the
    #: emulator would have to exist *inside* the target root, which is
    #: impossible before that root has been bootstrapped.
    fix_binary: bool = False
    registration: str = ""
    reason: str = ""

    def describe(self) -> str:
        if self.native:
            return f"архитектура {self.target_arch} совпадает с архитектурой этой машины"
        if not self.available:
            return self.reason
        return (
            f"архитектура {self.target_arch} чужая для этой машины; "
            f"используется эмуляция {self.interpreter}"
        )


def _read_registration(path: str) -> tuple[str, str, bool]:
    """Returns (status, interpreter, fix_binary) for one binfmt_misc entry."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return "", "", False
    status = lines[0] if lines else ""
    interpreter = ""
    fix_binary = False
    for line in lines[1:]:
        if line.startswith("interpreter "):
            interpreter = line.split(" ", 1)[1].strip()
        elif line.startswith("flags:"):
            fix_binary = "F" in line.split(":", 1)[1]
    return status, interpreter, fix_binary


def probe(target_arch: str, host_arch_name: str = "") -> Emulation:
    """Whether this machine can build a medium for `target_arch`."""
    arch = kernel_arch(target_arch)
    if is_native(arch, host_arch_name):
        return Emulation(target_arch=arch, native=True, available=True)

    if not os.path.isdir(BINFMT_DIR):
        return Emulation(
            target_arch=arch,
            native=False,
            available=False,
            reason=(
                f"ядро не предоставляет {BINFMT_DIR}, поэтому запускать программы "
                f"архитектуры {arch} на этой машине нечем. Обычно это значит, что не "
                "загружен модуль binfmt_misc."
            ),
        )

    for entry in sorted(os.listdir(BINFMT_DIR)):
        if entry in ("register", "status"):
            continue
        status, interpreter, fix_binary = _read_registration(os.path.join(BINFMT_DIR, entry))
        # An entry is matched by the architecture token in its own name or in
        # the interpreter's path (`qemu-x86_64`, `x86_64-binfmt-P`), rather than
        # by a fixed list of names -- registrations are made by whatever the
        # distribution ships, and this program has no business dictating them.
        if arch not in entry and arch not in interpreter:
            continue
        if status != "enabled":
            return Emulation(
                target_arch=arch,
                native=False,
                available=False,
                registration=entry,
                reason=(
                    f"эмулятор для {arch} зарегистрирован ({entry}), но выключен. "
                    f"Включить: echo 1 > {BINFMT_DIR}/{entry}"
                ),
            )
        if not fix_binary:
            # Without F the kernel resolves the interpreter path *inside* the
            # chroot, where it does not exist -- and it cannot be put there
            # before the root itself has been bootstrapped. Both Debian's
            # qemu-user-static and the RHEL family's register with F, so this
            # is a broken setup to report, not a case to work around with
            # code that would never run in practice.
            return Emulation(
                target_arch=arch,
                native=False,
                available=False,
                registration=entry,
                reason=(
                    f"эмулятор для {arch} зарегистрирован ({entry}) без флага F, "
                    "поэтому внутри chroot он недоступен. Обычно это признак ручной "
                    f"регистрации; переустановите {EMULATOR_PACKAGE}."
                ),
            )
        return Emulation(
            target_arch=arch,
            native=False,
            available=True,
            interpreter=interpreter,
            fix_binary=fix_binary,
            registration=entry,
        )

    return Emulation(
        target_arch=arch,
        native=False,
        available=False,
        reason=(
            f"на этой машине ({host_kernel_arch()}) не зарегистрирован эмулятор для "
            f"архитектуры {arch}, поэтому собрать такой носитель нельзя. "
            f"Установите пакет {EMULATOR_PACKAGE} (в Debian вместе с binfmt-support) "
            "и повторите."
        ),
    )
