"""LUKS2 helpers ("Установка.LUKS", "Установка.LUKS.Keyslot.*").

Passphrases are always handed to `cryptsetup` through a 0600 temp file
(see command_runner.secret_file), never as argv text, so they never show up
in `ps` output or shell history.
"""
from __future__ import annotations

import contextlib

from .command_runner import CommandPlan, CommandRunner, secret_file

DEFAULT_CIPHER = "aes-xts-plain64"
DEFAULT_KEY_SIZE = 512


@contextlib.contextmanager
def _key_path(data: bytes, dry_run: bool):
    """Avoids ever writing secret material to disk during dry runs (plan
    preview / tests) -- only a real (non-dry) invocation touches disk, via
    the 0600-permission secret_file() helper."""
    if dry_run:
        yield "<dry-run-keyfile>"
    else:
        with secret_file(data) as path:
            yield path


class LuksManager:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or CommandRunner()

    def format(
        self,
        partition: str,
        passphrase: bytes,
        *,
        cipher: str = DEFAULT_CIPHER,
        key_size: int = DEFAULT_KEY_SIZE,
        dry_run: bool = True,
        confirm_device: str | None = None,
        plan: CommandPlan | None = None,
    ) -> CommandPlan:
        """Implements "Установка.LUKS": formats `partition` as LUKS2 and adds
        the initial (user) keyslot in the same call.

        `plan` lets a caller (the installer) funnel these commands into its own
        plan. That is not cosmetic: CommandRunner gates real execution on
        `confirm_device == plan.device`, so a plan created here with
        device=/dev/sda2 could never match the /dev/sda the user confirmed, and
        every real run died at the very first cryptsetup call.
        """
        plan = plan if plan is not None else CommandPlan(device=partition)
        with _key_path(passphrase, dry_run) as key_path:
            argv = [
                "cryptsetup", "luksFormat", "--type", "luks2",
                "--cipher", cipher, "--key-size", str(key_size),
                "--key-file", str(key_path), "--batch-mode", partition,
            ]
            self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device)
        return plan

    def add_keyslot(
        self,
        partition: str,
        existing_passphrase: bytes,
        new_passphrase: bytes,
        *,
        slot: int | None = None,
        dry_run: bool = True,
        confirm_device: str | None = None,
        plan: CommandPlan | None = None,
    ) -> CommandPlan:
        """Implements "Установка.LUKS.Keyslot.Recovery" (and re-keying after a
        successful recovery, mirrored in recovery_module.sh)."""
        plan = plan if plan is not None else CommandPlan(device=partition)
        with _key_path(existing_passphrase, dry_run) as existing_path, _key_path(new_passphrase, dry_run) as new_path:
            argv = ["cryptsetup", "luksAddKey", partition, "--key-file", str(existing_path)]
            if slot is not None:
                argv += ["--key-slot", str(slot)]
            argv.append(str(new_path))
            self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device)
        return plan

    def open(
        self,
        partition: str,
        mapper_name: str,
        passphrase: bytes,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
        plan: CommandPlan | None = None,
    ) -> CommandPlan:
        """Unlocks the LUKS container so a filesystem can be created inside it.

        Without this the installer formatted an encrypted partition and then
        never opened it, so the target system was bootstrapped into a plain
        directory on the *host* instead of onto the medium.
        """
        plan = plan if plan is not None else CommandPlan(device=partition)
        with _key_path(passphrase, dry_run) as key_path:
            argv = [
                "cryptsetup", "luksOpen", partition, mapper_name,
                "--key-file", str(key_path),
            ]
            self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device)
        return plan

    def close(
        self,
        mapper_name: str,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
        plan: CommandPlan | None = None,
    ) -> CommandPlan:
        plan = plan if plan is not None else CommandPlan(device=mapper_name)
        argv = ["cryptsetup", "luksClose", mapper_name]
        self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device)
        return plan

    def kill_slot(
        self,
        partition: str,
        slot: int,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
        plan: CommandPlan | None = None,
    ) -> CommandPlan:
        plan = plan if plan is not None else CommandPlan(device=partition)
        argv = ["cryptsetup", "luksKillSlot", partition, str(slot), "--batch-mode"]
        self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device)
        return plan

    @staticmethod
    def mapper_path(mapper_name: str) -> str:
        return f"/dev/mapper/{mapper_name}"

    @staticmethod
    def uuid_command(partition: str) -> list[str]:
        return ["cryptsetup", "luksUUID", partition]
