"""Действия над выданным носителем: список, поиск, имя, целостность, журналы.

Вторая половина слоя служб. Первая -- `media_release.py` -- отвечает на вопрос
«как выпустить носитель»; эта отвечает на вопрос «что с уже выпущенным делать».

Зачем. Четыре экрана лезли в контейнер сами: панель брала
`container.data.media_records` и правила поле записи, журналы и обмен брали тот
же список третьим и четвёртым способом, а проверка целостности вынимала
`container.admin_secret` прямо в интерфейс -- тридцать строк разбора подписи,
монтирования томов и сравнения сумм жили в обработчике кнопки.

Секрет администратора в экране -- это не стилистика. Всё, что его касается,
должно лежать там, где это видно одним взглядом: в слое служб. Экран, который
умеет достать секрет, рано или поздно покажет его, запишет в журнал или отдаст
в отчёт -- не по злому умыслу, а потому что он рядом.

Чего здесь намеренно **нет**:

* никакого показа и никаких сообщений человеку -- служба возвращает факты и
  бросает исключения, а слова подбирает экран. Одна и та же нехватка носителя
  звучит по-разному в проверке целостности и в журналах;
* никакого кэша списка носителей. Панель опрашивает порты каждые пять секунд,
  и список, застрявший в службе, показывал бы флешку, которую уже вынули.
"""
from __future__ import annotations

from ..container import ContainerManager
from ..errors import PortableOSAdminError
from ..install import integrity
from ..session import AdminSession
from . import media_detect
from .logs import LogViewer
from .service_volumes import PlainVolume, open_logs, partition_by_label


class MediaActions:
    """Действия над носителями открытого контейнера администратора."""

    def __init__(self, session: AdminSession, containers: "ContainerManager | None" = None) -> None:
        self.session = session
        #: Сохранение контейнера самодостаточно -- путь лежит в самом
        #: контейнере, -- поэтому служба сохраняет сама, а не просит об этом
        #: окно. Иначе политика сохранения жила бы в двух местах.
        self.containers = containers or ContainerManager()

    @property
    def container(self):
        return self.session.container

    # -- учёт ---------------------------------------------------------------
    def records(self) -> tuple:
        """Записи о носителях -- в порядке выдачи.

        Возвращается снимок последовательности, а не она сама: экран, который
        по недосмотру удалит из неё строку, удалит её из контейнера. Сами
        записи -- те же объекты, и правка записи (например, имени) остаётся
        видимой всем, кто её держит.
        """
        return tuple(self.container.data.media_records)

    def find(self, media_id: str):
        """Запись по идентификатору или `None`, если такой нет."""
        if not media_id:
            return None
        return self.container.find_media(media_id)

    def connected(self, records=None) -> dict:
        """Какие из носителей сейчас воткнуты -- по UUID их томов.

        По UUID, а не по пути устройства: та же флешка на этой машине
        `/dev/sdb`, на соседней `/dev/sdc`. Пароль и монтирование не нужны.
        """
        known = self.records() if records is None else tuple(records)
        return media_detect.detect_map(known)

    def rename(self, media_id: str, name: str):
        """Дать носителю имя и сохранить контейнер.

        Имя живёт в контейнере, а не на флешке, поэтому переименовать можно
        уже выданный, потерянный и даже сломанный носитель -- лишь бы запись о
        нём была здесь.

        Сохранение -- часть действия, а не отдельный шаг для вызывающего.
        Переименование, не дошедшее до диска, выглядит на экране успешным и
        пропадает при закрытии программы.
        """
        record = self.find(media_id)
        if record is None:
            raise PortableOSAdminError("Запись о носителе не найдена.")
        record.name = name.strip()
        self.containers.save(self.container)
        return record

    # -- проверка незашифрованной части -------------------------------------
    def check_integrity(self, record) -> "dict[str, list[str]]":
        """Сверить ESP и `/boot` носителя с описью, снятой при записи.

        Это обнаружение, а не защита: подменённый initramfs спрашивает пароль
        так же, как настоящий, и если человек уже загрузился с такого носителя,
        пароль украден до всякой проверки.
        """
        expected = dict(record.boot_manifest or {})
        if not expected:
            raise PortableOSAdminError(
                "У этого носителя нет описи: он записан версией программы, "
                "которая её ещё не снимала. Сравнивать не с чем."
            )

        # Подпись проверяется до сравнения, а не после. Опись лежит в
        # зашифрованном контейнере, но проверить её всё равно стоит: файл
        # мог быть склеен из двух, и тогда сравнение шло бы с чужим эталоном.
        if not integrity.verify(
            expected, record.boot_manifest_signature, self.container.admin_secret
        ):
            raise PortableOSAdminError(
                "Подпись описи не сходится: она составлена не этим контейнером."
            )

        found = self.connected([record]).get(record.media_id)
        if found is None:
            raise PortableOSAdminError(
                f"Носитель «{record.display_name}» не найден среди подключённых. "
                "Вставьте его и повторите."
            )

        esp_partition = partition_by_label(found.device_path, "POA_ESP")
        boot_partition = partition_by_label(found.device_path, "POA_BOOT")
        # Оба тома живут ровно столько, сколько считаются суммы, и
        # размонтируются в любом случае -- в том числе когда подсчёт не удался.
        with PlainVolume(esp_partition) as esp, PlainVolume(boot_partition) as boot:
            actual = integrity.collect({"boot": boot.mountpoint, "esp": esp.mountpoint})
        return integrity.compare(expected, actual)

    # -- журналы ------------------------------------------------------------
    def read_logs(self, record, filter_text: "str | None" = None) -> "list[str]":
        """Прочитать журналы носителя, открыв его том ключом из контейнера.

        Том открывается на чтение ровно на время чтения и закрывается в любом
        случае: пользователь переносной системы не должен уметь править
        журналы, и инструмент администратора не должен открывать их на запись.
        """
        with open_logs(record) as volume:
            return LogViewer().read_logs(volume.mountpoint, filter_substring=filter_text)
