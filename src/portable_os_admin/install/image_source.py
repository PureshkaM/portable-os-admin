"""Using a local distribution image as the source of the installed system.

The installer takes the base system from an ISO the administrator already has,
never from the network. That is a requirement, not an optimisation: this tool is
meant for networks with no route to the internet, where downloading a
distribution during installation is simply not possible.

Both supported image kinds are ordinary package repositories once mounted, so
nothing has to be extracted: a Debian DVD carries `dists/<suite>/…/Packages.gz`
plus `pool/`, and a RHEL-family image carries `repodata/`. Pointing the
family's own bootstrapper at `file:///…` is all that is needed -- verified
against a real Debian 13.6 DVD, which holds 4447 packages including the kernel
and GRUB, so a complete bootable system can be produced with the network
unplugged.

Package *installation* beyond the base system may still use a network or a
company mirror; that is the administrator's choice and unrelated to this file.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

from ..errors import InstallError
from .tooling import resolve_argv


@dataclass(frozen=True)
class ImageInfo:
    """What a distribution image turned out to contain.

    Everything here is read out of the image, never guessed from its filename
    and never taken from a list of releases built into this program. That is
    deliberate: hard-coding known versions would mean a new release of any
    target distribution needs a change here before it can be used at all.
    """

    path: str
    #: "debian" or "redos" -- which backend family the image can install.
    family: str
    #: Release identifier the family's bootstrapper expects: a Debian codename
    #: ("trixie"), or a RHEL-family version ("8.0").
    release: str
    #: Architectures the image carries packages for.
    architectures: tuple[str, ...]
    #: Human-readable name, taken from the image itself rather than the filename.
    label: str
    #: Repository components the image publishes ("main", "contrib", ...),
    #: read from its own Release file. Needed to point the target's package
    #: manager at the image, and read rather than assumed because an image may
    #: carry only main, or main plus firmware, depending on how it was built.
    components: tuple[str, ...] = ("main",)
    #: Whether a complete system can be installed from this image with no
    #: network at all. False for network-install images.
    complete: bool = True
    #: Why it is not complete, in a form that can be shown to a person.
    incomplete_reason: str = ""

    def describe(self) -> str:
        arch = ", ".join(self.architectures) if self.architectures else "?"
        return f"{self.label} — {self.family} {self.release}, {arch}"


class MountedImage:
    """Mounts an image read-only for the duration of a `with` block.

    Read-only and `nodev,nosuid` are not decoration: the image is mounted so
    that a package manager can read from it, and nothing on it should ever be
    executable-as-device or setuid from this machine's point of view.
    """

    def __init__(self, path: str, mountpoint: str | None = None):
        self.path = path
        self.mountpoint = mountpoint or tempfile.mkdtemp(prefix="poa-image-")
        self._own_mountpoint = mountpoint is None
        self._mounted = False

    def __enter__(self) -> "MountedImage":
        self.mount()
        return self

    def __exit__(self, *_exc) -> None:
        self.unmount()

    @property
    def url(self) -> str:
        """The mounted image as a repository URL for apt/dnf."""
        return f"file://{self.mountpoint}"

    def mount(self) -> None:
        if self._mounted:
            return
        # Remember whether this call is what brought the directory into
        # existence, so unmount() can take it away again. The constructor's
        # `_own_mountpoint` only says whether the *path* was chosen here, which
        # is a different question -- and getting the two confused left a stale
        # /mnt/poa-image-<id> behind after every single install.
        self._created_mountpoint = not os.path.isdir(self.mountpoint)
        os.makedirs(self.mountpoint, exist_ok=True)
        proc = subprocess.run(
            resolve_argv(["mount", "-o", "loop,ro,nodev,nosuid", self.path, self.mountpoint]),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            raise InstallError(
                f"не удалось подключить образ {self.path}: {proc.stderr.strip() or proc.returncode}"
            )
        self._mounted = True

    def unmount(self) -> None:
        if not self._mounted:
            return
        # A plain umount first, and only then the lazy one. `umount -l` detaches
        # the tree and finishes the work later, so the rmdir below could run
        # while the mount was still going away and fail with EBUSY -- which is
        # how empty mount points accumulated one per install.
        proc = subprocess.run(
            resolve_argv(["umount", self.mountpoint]), capture_output=True, timeout=60, check=False
        )
        if proc.returncode != 0:
            subprocess.run(
                resolve_argv(["umount", "-l", self.mountpoint]),
                capture_output=True,
                timeout=60,
                check=False,
            )
        self._mounted = False
        if self._own_mountpoint or getattr(self, "_created_mountpoint", False):
            try:
                os.rmdir(self.mountpoint)
            except OSError:
                # Still busy, or not empty: leave it rather than fight. The
                # medium is finished either way, and an orphaned empty
                # directory is reported by the "Проверить состояние" check.
                pass


def probe(path: str) -> ImageInfo:
    """Mounts the image, works out what it is, and unmounts it again.

    Deliberately reports what the image *says about itself* rather than trusting
    the filename: an image named for one release but built from another would
    otherwise produce a medium that fails to boot for no visible reason.
    """
    if not os.path.isfile(path):
        raise InstallError(f"файл образа не найден: {path}")
    with MountedImage(path) as image:
        return inspect_mounted(path, image.mountpoint)


def inspect_mounted(path: str, mountpoint: str) -> ImageInfo:
    debian = _inspect_debian(path, mountpoint)
    if debian is not None:
        return debian
    redhat = _inspect_redhat(path, mountpoint)
    if redhat is not None:
        return redhat
    raise InstallError(
        f"не удалось распознать образ {os.path.basename(path)}: не найдено ни репозитория Debian "
        "(каталог dists/), ни репозитория RPM (каталог repodata/). "
        "Нужен установочный образ дистрибутива, а не Live-образ и не образ восстановления."
    )


def _inspect_debian(path: str, mountpoint: str) -> ImageInfo | None:
    dists = os.path.join(mountpoint, "dists")
    if not os.path.isdir(dists):
        return None

    # "stable" is a symlink to the codename on official images; following the
    # real directory gives the codename debootstrap actually wants.
    for entry in sorted(os.listdir(dists)):
        suite_dir = os.path.join(dists, entry)
        release_file = os.path.join(suite_dir, "Release")
        if os.path.islink(suite_dir) or not os.path.isfile(release_file):
            continue
        fields = _parse_release(release_file)
        architectures = tuple(fields.get("Architectures", "").split())
        index = _package_index(suite_dir, architectures)
        if index is None:
            continue
        complete, reason = _debian_completeness(mountpoint, index, architectures)
        return ImageInfo(
            path=path,
            family="debian",
            release=fields.get("Codename") or entry,
            architectures=architectures,
            components=_usable_components(suite_dir, fields, architectures),
            label=_disk_info(mountpoint) or f"Debian {fields.get('Version', '')}".strip(),
            complete=complete,
            incomplete_reason=reason,
        )
    return None


def _usable_components(suite_dir: str, fields: dict, architectures: tuple[str, ...]) -> tuple[str, ...]:
    """Components the image declares *and* actually carries for this
    architecture.

    apt drops a whole repository when one declared component has no index for
    the architecture in use, so a Release file that over-declares would cost
    the entire install. Filtering here rather than trusting the declaration
    turns that into a component quietly not being offered.
    """
    declared = fields.get("Components", "main").split()
    arch = architectures[0] if architectures else ""
    usable = [
        name for name in declared
        if os.path.isdir(os.path.join(suite_dir, name, f"binary-{arch}"))
    ]
    return tuple(usable) or ("main",)


def _package_index(suite_dir: str, architectures: tuple[str, ...]) -> str | None:
    """Path of the suite's package index, or None if this is not a repository."""
    for arch in architectures or ("",):
        binary_dir = os.path.join(suite_dir, "main", f"binary-{arch}")
        if not os.path.isdir(binary_dir):
            continue
        for name in sorted(os.listdir(binary_dir)):
            if name.startswith("Packages"):
                return os.path.join(binary_dir, name)
    return None


def _debian_completeness(mountpoint: str, index: str, architectures: tuple[str, ...]) -> tuple[bool, str]:
    """Can a *bootable* system be installed from this image with no network?

    Tested by what the image actually holds rather than by its name or type
    marker: the medium needs a kernel and an EFI bootloader, and if the image
    cannot supply them the install fails an hour in, halfway through. A
    network-install image is exactly the case this rejects -- it carries the
    base system but not the kernel and bootloader packages.
    """
    packages = _index_package_names(index)
    if not packages:
        return False, "не удалось прочитать список пакетов образа"

    arch = architectures[0] if architectures else ""
    missing = []
    if not any(name.startswith("linux-image-") for name in packages):
        missing.append("ядро (linux-image-*)")
    if not any(name.startswith("grub-efi-") for name in packages):
        missing.append("загрузчик (grub-efi-*)")
    if missing:
        return False, (
            "образ не содержит " + " и ".join(missing) + ", поэтому загружаемую систему "
            "с него собрать нельзя. Похоже на образ сетевой установки (netinst) — "
            f"нужен полный установочный образ{' для ' + arch if arch else ''}."
        )
    return True, ""


def _index_package_names(index: str) -> set[str]:
    """Package names from an apt index, transparently handling its compression.

    Only the `Package:` lines are read; the index is tens of megabytes and
    nothing else here is needed.
    """
    openers = {".gz": _open_gz, ".xz": _open_xz}
    opener = openers.get(os.path.splitext(index)[1], open)
    names: set[str] = set()
    try:
        with opener(index) as handle:  # type: ignore[operator]
            for raw in handle:
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if line.startswith("Package: "):
                    names.add(line[9:].strip())
    except (OSError, ValueError):
        return set()
    return names


def _open_gz(path: str):
    import gzip

    return gzip.open(path, "rb")


def _open_xz(path: str):
    import lzma

    return lzma.open(path, "rb")


def _inspect_redhat(path: str, mountpoint: str) -> ImageInfo | None:
    if not os.path.isdir(os.path.join(mountpoint, "repodata")):
        return None
    family, release, arch, name = "redos", "", "", ""
    treeinfo = os.path.join(mountpoint, ".treeinfo")
    if os.path.isfile(treeinfo):
        fields = _parse_ini(treeinfo)
        release = fields.get("version", "")
        arch = fields.get("arch", "")
        name = fields.get("name", "")
    if not release:
        # .discinfo's second line is the product name and version.
        discinfo = os.path.join(mountpoint, ".discinfo")
        if os.path.isfile(discinfo):
            with open(discinfo, encoding="utf-8", errors="replace") as handle:
                lines = [line.strip() for line in handle.readlines()]
            if len(lines) > 1:
                match = re.search(r"(\d+(?:\.\d+)*)", lines[1])
                if match:
                    release = match.group(1)
    # A RHEL-family boot/network-install image carries the installer but no
    # repository, so the presence of repodata/ above is already what separates
    # a full image from one that would need the network.
    complete = os.path.isdir(os.path.join(mountpoint, "Packages")) or _repodata_has_packages(mountpoint)
    return ImageInfo(
        path=path,
        family=family,
        release=release,
        architectures=(arch,) if arch else (),
        label=name or _disk_info(mountpoint) or f"RedOS {release}".strip(),
        complete=complete,
        incomplete_reason=(
            ""
            if complete
            else "образ содержит метаданные репозитория, но не сами пакеты — "
            "похоже на загрузочный образ сетевой установки, нужен полный установочный образ"
        ),
    )


def _repodata_has_packages(mountpoint: str) -> bool:
    """True when the image's repository actually holds packages.

    Judged by repomd.xml declaring a non-trivial primary index: parsing the
    compressed primary itself would mean pulling in zstd handling for a
    question that its size already answers.
    """
    repomd = os.path.join(mountpoint, "repodata", "repomd.xml")
    try:
        return os.path.getsize(repomd) > 0 and any(
            not name.startswith("repomd") and os.path.getsize(os.path.join(mountpoint, "repodata", name)) > 100_000
            for name in os.listdir(os.path.join(mountpoint, "repodata"))
        )
    except OSError:
        return False


def _parse_release(path: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith((" ", "\t")) or ":" not in line:
                continue  # checksum blocks are indented; skip them
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _parse_ini(path: str) -> dict[str, str]:
    """Flat key=value read of .treeinfo, ignoring sections.

    Section-blind on purpose: the two keys wanted here (version, arch) appear
    once in [general] on every image seen, and a full parser would add nothing.
    """
    fields: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith(("#", "[")) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields.setdefault(key.strip(), value.strip())
    return fields


def _disk_info(mountpoint: str) -> str:
    """The image's own name, as it states it.

    Debian puts a readable description on the first line of `.disk/info`;
    RHEL-family `.discinfo` puts a build timestamp there and the product name
    on the *second* line, so taking line one for both would label a RedOS image
    "1752250767.418455".
    """
    debian = os.path.join(mountpoint, ".disk", "info")
    if os.path.isfile(debian):
        with open(debian, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
        if first:
            return first

    discinfo = os.path.join(mountpoint, ".discinfo")
    if os.path.isfile(discinfo):
        with open(discinfo, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle.readlines()]
        if len(lines) > 1 and lines[1]:
            return lines[1]
    return ""
