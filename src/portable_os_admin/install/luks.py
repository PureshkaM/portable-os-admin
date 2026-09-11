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
#: Размер сектора шифрования, байт.
#:
#: По умолчанию cryptsetup берёт 512 — размер логического сектора, о котором
#: сообщает устройство. Но страница флеш-памяти давно не 512 байт, а 16 КБ и
#: больше, и каждая запись по 512 байт превращается для контроллера в
#: чтение-изменение-запись целой страницы. Замер на тестовой флешке: носитель
#: держит 2,9 МБ/с последовательной записи, а развёртывание шло со скоростью
#: 0,49 МБ/с — вшестеро медленнее, и это одна из причин.
#:
#: 4096 — размер, с которым работают все современные ФС; устройства с
#: логическим сектором 512 его принимают.
DEFAULT_SECTOR_SIZE = 4096
DEFAULT_KDF = "argon2id"

#: Потолок стоимости argon2id по памяти, КиБ.
#:
#: cryptsetup подбирает эту стоимость под ту машину, где том создаётся, и
#: записывает её в заголовок LUKS навсегда. Для обычного диска это правильно:
#: он останется в той же машине. Для переносного носителя — нет. Носитель,
#: собранный на машине с 32 ГБ памяти, потребует при разблокировке больше,
#: чем есть на машине, куда его принесли, и откроется медленно или не
#: откроется вовсе. Наблюдалось: «Warning: keyslot operation could fail as it
#: requires more than available memory» при загрузке в машине с 2 ГБ.
#:
#: 256 МиБ — значение, выбранное пользователем 26.08.2026: для argon2id это
#: по-прежнему стойко, и столько памяти есть у любой машины, на которой имеет
#: смысл запускать этот носитель. Число захардкожено намеренно, в
#: конфигурацию не вынесено.
MAX_PBKDF_MEMORY_KIB = 262144

#: Число потоков argon2id, записываемое в заголовок.
#:
#: Та же болезнь, что и с памятью, только незаметнее. cryptsetup берёт число
#: потоков по числу ядер сборочной машины — на восьмиядерной рабочей станции
#: в заголовок уходит 4. Носитель уносят в терминал с двумя ядрами, и те же
#: четыре потока считаются вдвое дольше: стоимость подобрана под чужое железо.
#:
#: 2 — то, что есть у любой машины, на которой имеет смысл запускать этот
#: носитель. Стойкость от этого не страдает: у argon2id её определяет память,
#: а не число дорожек.
PBKDF_PARALLEL = 2

#: Число проходов argon2id, записываемое в заголовок.
#:
#: Третья и последняя из тех же граблей. Память и потоки здесь уже
#: зафиксированы, а число проходов оставалось на откуп cryptsetup -- и он
#: подбирает его так, чтобы уложиться в две секунды **на машине, где создаётся
#: слот**. Замерено 31.08.2026: на машине администратора в заголовок уходит
#: `Time cost: 40`, и ровно во столько же раз дороже становится каждая
#: проверка пароля на машине пользователя.
#:
#: Цена была видна во всём: минуты на разблокировку вместо секунд, «зависшая»
#: на восемнадцать минут регистрация компьютера, семь минут на двадцать девять
#: слотов при развёртывании. Создание слота с фиксированным числом проходов --
#: 2,2 секунды против 6,4.
#:
#: 4 при 256 МиБ -- обычная величина для argon2id: RFC 9106 предлагает от
#: одного прохода при большой памяти. Стойкость держит память, а не проходы:
#: атакующему нужно 256 МиБ на каждую попытку, и это тот барьер, ради которого
#: argon2id и выбран.
PBKDF_ITERATIONS = 4


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
        kdf: str = DEFAULT_KDF,
        sector_size: int = DEFAULT_SECTOR_SIZE,
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
                # --pbkdf was previously omitted, so config.luks.kdf was a
                # field the user could set that changed nothing: cryptsetup
                # silently used its own default instead.
                "--pbkdf", kdf,
                "--pbkdf-memory", str(MAX_PBKDF_MEMORY_KIB),
                "--pbkdf-parallel", str(PBKDF_PARALLEL),
                "--pbkdf-force-iterations", str(PBKDF_ITERATIONS),
                "--sector-size", str(sector_size),
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
            # Тот же потолок, что и при создании тома: каждый keyslot несёт
            # собственную стоимость, и слот, добавленный без ограничения,
            # свёл бы всю меру на нет — носитель открылся бы пользовательским
            # паролем, но не служебным ключом или кодом восстановления.
            argv = [
                "cryptsetup", "luksAddKey", partition,
                "--pbkdf-memory", str(MAX_PBKDF_MEMORY_KIB),
                "--pbkdf-parallel", str(PBKDF_PARALLEL),
                "--pbkdf-force-iterations", str(PBKDF_ITERATIONS),
                "--key-file", str(existing_path),
            ]
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

    def erase_header(
        self,
        partition: str,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
        plan: CommandPlan | None = None,
    ) -> CommandPlan:
        """Уничтожает все keyslot'ы тома -- то есть сам мастер-ключ.

        Это и есть настоящий отзыв вернувшегося носителя, в отличие от отметки
        в контейнере. Мастер-ключ нигде не хранится открытым: в заголовке лежат
        его копии, зашифрованные на каждом слоте, и других копий нет. Уничтожив
        все слоты, том не открыть ничем -- ни паролем пользователя, ни
        служебным ключом из контейнера администратора, ни кодом восстановления.
        Данные после этого не восстанавливаются, и обратного действия у этой
        команды нет.

        Сам заголовок при этом остаётся, и UUID тома в нём -- тоже. Намеренно:
        по UUID носитель узнаётся в порту (`admin/media_detect.py`), и стёртая
        флешка должна по-прежнему называть себя той самой записью, а не
        превращаться в неопознанную. Затирать сигнатуру целиком (`wipefs`)
        значило бы усложнить администратору ровно тот случай, ради которого всё
        и делается: понять, что за носитель ему принесли.
        """
        plan = plan if plan is not None else CommandPlan(device=partition)
        argv = ["cryptsetup", "luksErase", partition, "--batch-mode"]
        self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device)
        return plan

    @staticmethod
    def mapper_path(mapper_name: str) -> str:
        return f"/dev/mapper/{mapper_name}"

    @staticmethod
    def uuid_command(partition: str) -> list[str]:
        return ["cryptsetup", "luksUUID", partition]
