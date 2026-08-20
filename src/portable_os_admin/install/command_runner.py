"""Shared "dry-run by default" execution gate for every destructive command
used by the installer (parted/sgdisk, cryptsetup, mkfs, debootstrap, dnf...).

Every real filesystem/disk mutation in this project funnels through
`CommandRunner.run`, which:
  - refuses to execute anything unless `dry_run=False`
  - additionally requires the caller to repeat the exact target device path
    as `confirm_device`, as a lightweight guard against acting on the wrong
    disk from a mis-clicked UI

This exists because none of this code has been run against a real block
device yet (see docs/ARCHITECTURE.md "Known limitations" -- this project was
built in a sandbox with no spare disks/USB media to test destructive
operations against). Treat any dry_run=False path as unverified until
confirmed on real hardware.
"""
from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InstallError

DEFAULT_TIMEOUT = 600
# debootstrap/dnf pull a whole base system over the network onto slow USB
# flash; ten minutes is routinely not enough, and being killed halfway leaves
# a half-written medium, so bootstrapping gets its own much larger budget.
BOOTSTRAP_TIMEOUT = 7200


@dataclass
class CommandResult:
    argv: list[str]
    executed: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass
class CommandPlan:
    """Accumulates the commands a higher-level operation *would* run, so the
    whole plan can be inspected/logged/tested before anything destructive
    happens."""

    device: str
    results: list[CommandResult] = field(default_factory=list)

    def describe(self) -> str:
        return "\n".join(" ".join(r.argv) for r in self.results)


class CommandRunner:
    def run(
        self,
        argv: list[str],
        plan: CommandPlan,
        *,
        dry_run: bool,
        confirm_device: str | None,
        input_bytes: bytes | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> CommandResult:
        if not dry_run:
            if confirm_device != plan.device:
                raise InstallError(
                    "подтверждение целевого устройства не совпадает — операция отменена "
                    f"(ожидалось {plan.device!r}, получено {confirm_device!r})"
                )
            proc = subprocess.run(
                argv, input=input_bytes, capture_output=True, timeout=timeout, check=False
            )
            result = CommandResult(
                argv=argv,
                executed=True,
                returncode=proc.returncode,
                stdout=proc.stdout.decode("utf-8", errors="replace"),
                stderr=proc.stderr.decode("utf-8", errors="replace"),
            )
            plan.results.append(result)
            if proc.returncode != 0:
                raise InstallError(f"команда завершилась с ошибкой ({proc.returncode}): {' '.join(argv)}\n{result.stderr}")
            return result

        result = CommandResult(argv=argv, executed=False)
        plan.results.append(result)
        return result


@contextlib.contextmanager
def secret_file(data: bytes, directory: str = "/dev/shm"):
    """Writes a secret (LUKS passphrase, recovery passphrase, ...) to a 0600
    file for handoff to cryptsetup via --key-file=<path>, avoiding both argv
    exposure (visible in `ps`) and the double-stdin problem of piping two
    different secrets into one invocation. Falls back to a regular temp
    directory if /dev/shm (tmpfs) is not available, e.g. outside Linux."""
    base_dir = directory if os.path.isdir(directory) else tempfile.gettempdir()
    fd, path = tempfile.mkstemp(prefix=".poa-secret-", dir=base_dir)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        yield Path(path)
    finally:
        try:
            with open(path, "r+b") as handle:
                length = handle.seek(0, os.SEEK_END)
                handle.seek(0)
                handle.write(b"\0" * length)
        except OSError:
            pass
        os.unlink(path)
