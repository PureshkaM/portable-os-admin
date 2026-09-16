"""Выпуск носителя: одна служба на три пути.

Зачем. Три мастера интерфейса делали одно и то же: каждый сам создавал
`OSInstaller`, сам звал у него свой метод и сам держал ссылку ради `abort()`.
Экран знал, как устроен установщик, -- и это ровно тот класс связи, из-за
которого правка в одном пути выпуска молча не доезжала до другого (четыре
дефекта 13.09.2026, долг 33 в `docs/PLAN.md`).

Теперь между экраном и установщиком стоит служба. Экран говорит, что нужно
выпустить, и получает результат; как это делается -- дело службы.

Что здесь намеренно **не** появилось:

* никакой своей логики выпуска -- служба не решает, как размечать диск и чем
  шифровать тома, она зовёт установщик. Появись здесь хоть одно собственное
  решение, их стало бы два места вместо одного;
* никакого сглаживания ошибок -- исключения установщика проходят насквозь.
  Экран показывает причину человеку, и подменять её общими словами нельзя:
  «не удалось записать носитель» вместо «устройство занято процессом 1234»
  превращает разбор в гадание.

Три пути выпуска -- это три способа получить носитель, а не три разные вещи:

1. **Из установочного образа** -- полный цикл на одном устройстве.
2. **Сборка дистрибутива** -- готовый корень на диске администратора, из
   которого потом штампуются носители.
3. **Развёртывание дистрибутива** -- запись уже собранного на устройство.

Когда появится токен (см. `install/medium.py`), выбор вида носителя ляжет
именно сюда: служба -- то место, которое знает, что именно выпускается.
"""
from __future__ import annotations

from ..install import BlockDevice, OSInstaller
from ..install.medium import MediumProfile, profile as medium_profile
from ..config.schema import InstallConfig
from ..session import AdminSession


class MediaReleaseService:
    """Выпуск носителя от имени открытого контейнера администратора.

    Живёт ровно столько, сколько идёт одна операция: установщик держит в себе
    состояние уборки (что смонтировано, какие тома открыты), и переиспользовать
    его между записями нельзя.
    """

    def __init__(self, session: AdminSession, medium: "MediumProfile | None" = None) -> None:
        self.session = session
        #: Вид носителя. Сегодня он один -- флешка; параметр существует, чтобы
        #: при появлении токена выбор делался здесь, а не в трёх мастерах.
        self.medium = medium or medium_profile()
        self._installer: "OSInstaller | None" = None
        self._abort_requested = False

    # -- три пути выпуска -------------------------------------------------
    def install_from_image(
        self,
        device: BlockDevice,
        config: InstallConfig,
        user_passphrase: bytes,
        *,
        image_path: str = "",
        dry_run: bool = True,
        confirm_device: "str | None" = None,
        on_step=None,
        media_name: str = "",
        media_owner: str = "",
    ):
        """Полный цикл: разметка, шифрование, система, загрузчик -- на устройство."""
        installer = self._new_installer()
        return installer.run(
            device,
            config,
            user_passphrase,
            image_path=image_path,
            dry_run=dry_run,
            confirm_device=confirm_device,
            on_step=on_step,
            media_name=media_name,
            media_owner=media_owner,
        )

    def build_distribution(
        self,
        config: InstallConfig,
        destination: str,
        *,
        image_path: str = "",
        extra_packages: "list[str] | None" = None,
        dry_run: bool = True,
        on_step=None,
    ):
        """Собрать дистрибутив: корень на диске администратора, без носителя."""
        installer = self._new_installer()
        return installer.build_distribution(
            config,
            destination,
            image_path=image_path,
            extra_packages=extra_packages,
            dry_run=dry_run,
            on_step=on_step,
        )

    def deploy_distribution(
        self,
        device: BlockDevice,
        directory: str,
        user_passphrase: bytes,
        *,
        dry_run: bool = True,
        confirm_device: "str | None" = None,
        on_step=None,
        media_name: str = "",
        media_owner: str = "",
        exchange_files: "list[str] | tuple[str, ...]" = (),
        accepted_signature=None,
    ):
        """Записать уже собранный дистрибутив на устройство."""
        installer = self._new_installer()
        return installer.deploy_distribution(
            device,
            directory,
            user_passphrase,
            dry_run=dry_run,
            confirm_device=confirm_device,
            on_step=on_step,
            media_name=media_name,
            media_owner=media_owner,
            exchange_files=exchange_files,
            accepted_signature=accepted_signature,
        )

    @property
    def installer(self) -> "OSInstaller | None":
        """Установщик идущей операции -- для пути отказа.

        Оба мастера записи спрашивают у него, тронут ли носитель: «прервано до
        первой разрушающей команды» и «носитель уже переписан» -- разные
        сообщения человеку, и подменять их общим «не удалось» нельзя. Служба
        границу не прячет там, где за ней стоит смысл.
        """
        return self._installer

    # -- общее ------------------------------------------------------------
    def abort(self) -> None:
        """Остановить идущую операцию.

        Зовётся из потока интерфейса, пока выпуск идёт в фоновом. Если ничего
        не идёт -- молчаливо ничего не делает: кнопка «Прервать» не должна
        падать оттого, что нажата на полсекунды позже, чем работа кончилась.

        Просьба запоминается. Между нажатием «Начать» и созданием установщика
        в фоновом потоке проходит мгновение, и раньше «Прервать», нажатое
        именно в него, пропадало совсем: останавливать было ещё нечего, а
        второй раз человек уже не нажмёт -- кнопка выглядит сработавшей. Теперь
        установщик, созданный после такой просьбы, останавливается сразу.
        """
        self._abort_requested = True
        installer = self._installer
        if installer is not None:
            installer.abort()

    @property
    def running(self) -> bool:
        return self._installer is not None

    def _new_installer(self) -> OSInstaller:
        """Свой установщик на каждую операцию -- см. пояснение к классу."""
        installer = OSInstaller(self.session)
        self._installer = installer
        if self._abort_requested:
            installer.abort()
        return installer
