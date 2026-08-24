"""Taking the configuration validator from the installation image itself.

An installation image is a full package repository, and the distribution's own
kickstart parser is a package in it. Using that copy is better than using
whatever happens to be on the administrator's machine for two reasons:

- it exists. `pykickstart` is not in Debian's archive at all, so a RedOS
  configuration authored on a Debian workstation had nothing to check it.
- it is the right version. The parser that ships on the image implements the
  syntax that image's installer accepts, which is the question actually being
  asked.

The package is `noarch` -- pure Python, no machine code -- so the
administrator's architecture is irrelevant here. This was measured: the parser
taken from a x86_64 RedOS 8 image runs on an arm64 Debian host and correctly
rejects an unknown command, a misspelt option and a missing required argument.

Nothing about the layout is hardcoded: the package is found by pattern, and
unpacked with whichever extractor the machine has.
"""
from __future__ import annotations

from pathlib import Path

from ..install.rpm_extract import PackageNotOnImageError, find_package, unpack

#: Package name patterns for the kickstart parser. RHEL splits it into
#: `pykickstart` (the CLI tools) and `python3-kickstart` (the library); only
#: the library is needed, and only the library is on the image.
PACKAGE_PATTERNS = ("*kickstart*.noarch.rpm", "*kickstart*.rpm")


class ValidatorNotOnImageError(RuntimeError):
    """The image carries no parser, or nothing here can unpack it."""


def module_search_path(unpacked_root: str) -> str:
    """Directory to put on PYTHONPATH so `import pykickstart` finds this copy.

    Found by looking for the package itself rather than by assuming
    site-packages, a Python version, or /usr/lib vs /usr/lib64.
    """
    for candidate in sorted(Path(unpacked_root).rglob("pykickstart")):
        if candidate.is_dir() and (candidate / "version.py").is_file():
            return str(candidate.parent)
    raise ValidatorNotOnImageError("в пакете с образа не нашлось модуля pykickstart")


def prepare(mountpoint: str, workdir: str) -> str:
    """Unpacks the image's kickstart parser and returns its PYTHONPATH entry."""
    try:
        package = find_package(mountpoint, PACKAGE_PATTERNS)
        unpack(package, workdir)
    except PackageNotOnImageError as exc:
        raise ValidatorNotOnImageError(
            f"{exc}. Проверить конфигурацию средством самого дистрибутива нельзя."
        ) from exc
    return module_search_path(workdir)
