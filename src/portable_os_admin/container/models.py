"""Data entities stored inside the encrypted admin container.

Maps to course-project § "Структура данных системы": "Запись носителя" and
"Recovery-запись" (table 6).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Состояния записи носителя.
#:
#: Их ровно два, и второе -- запись учёта, а не свойство самого носителя.
#: Отозванный носитель в руках у постороннего запирается не отметкой, а
#: привязкой к машинам и стиранием заголовков; отметка говорит только, что
#: администратор больше не считает его действующим. Интерфейс обязан называть
#: эту разницу вслух, иначе кнопка «отозвать» обещает защиту, которой нет.
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"


@dataclass
class MediaRecord:
    media_id: str
    luks_uuid: str
    #: Как носитель называется в системе: модель флешки или путь устройства.
    #: Заполняется при записи и остаётся техническим ответом на вопрос «что
    #: это за железка».
    label: str
    config_name: str
    target_os: str
    created_at: str = field(default_factory=_now_iso)
    #: Как носитель называет администратор: «Иванов, бухгалтерия».
    #:
    #: Идентификатор носителя (`media_id`) отвечает на вопрос «тот ли это
    #: носитель», но не отвечает на вопрос «чей он». В списке из десяти строк
    #: `46aed677` и `218d2d33` неразличимы, и администратор, которому надо
    #: отозвать носитель уволившегося сотрудника, не знает, какой из них
    #: выбрать. Имя задаётся при записи и меняется в любой момент: оно живёт в
    #: контейнере, а не на самом носителе, и переименование ничего не пишет на
    #: флешку.
    name: str = ""
    #: Кому носитель выдан: фамилия, имя и отчество сотрудника.
    #:
    #: Отдельно от `name`, хотя имя носителя часто содержит фамилию. Имя --
    #: как администратор носитель зовёт («Иванов, бухгалтерия», «запасной для
    #: склада»), владелец -- за кем он числится, и это разные сведения: имя
    #: меняют для удобства, владельца -- когда носитель передали другому
    #: человеку.
    #:
    #: Ради чего заведено: без владельца восстановление аутентифицирует
    #: носитель, а не человека. Нашедший чужую флешку доводит её до экрана с
    #: кодом запроса тремя неверными паролями -- носитель показывает код сам,
    #: -- и администратору звонит голос, про который известно ровно одно: он
    #: держит носитель в руках. Имя владельца превращает выдачу кода из
    #: «позвонил и получил» в «позвонил, был опознан». Дыру это закрывает не
    #: целиком, она процедурная, но администратору наконец есть что спросить.
    owner: str = ""
    #: Base64 of the key that opens this medium's log and exchange volumes.
    #: It lives here, inside the encrypted administrator container, and
    #: nowhere else on the administrator's machine -- losing the container
    #: means those two volumes can no longer be read from outside the portable
    #: system, which is the intended property, not an accident.
    service_key_b64: str = ""
    #: LUKS container UUIDs of those two volumes, so they can be found on a
    #: medium without trusting device names, which reorder between ports.
    logs_luks_uuid: str = ""
    exchange_luks_uuid: str = ""
    #: Опись незашифрованной части носителя: путь -> sha256, и подпись к ней.
    #:
    #: Эталон живёт здесь, а не на носителе, и в этом весь смысл: тот, кто
    #: подменил загрузчик, до контейнера администратора не дотянулся. Носитель
    #: несёт свою копию на зашифрованном корне -- ею он проверяет себя сам,
    #: но подделать обе разом можно только зная пароль и имея контейнер.
    boot_manifest: dict = field(default_factory=dict)
    boot_manifest_signature: str = ""
    #: Действует носитель или отозван -- и почему.
    #:
    #: Отозванная запись не удаляется: сотрудник уволился, а носитель остаётся
    #: фактом -- на нём стоит `media_id`, по нему приходят запросы
    #: восстановления, и он числится выданным в отчётности. Удалить запись
    #: значило бы стереть историю ради того, чтобы список выглядел коротким.
    #: Причину вводит администратор: через полгода «отозван» без причины не
    #: отвечает ни на один вопрос, ради которого в этот список смотрят.
    status: str = STATUS_ACTIVE
    revoked_at: str = ""
    revoked_reason: str = ""
    #: Номер последней команды, отправленной ресурсу отзыва про этот носитель.
    #:
    #: Строго возрастает и хранится у записи, а не общий на контейнер: сервер
    #: принимает команду, только если её номер больше уже известного ему. Без
    #: этого однажды подписанную команду «этот носитель в порядке» можно было
    #: бы прислать повторно и оживить отозванный.
    revocation_counter: int = 0
    #: Открытый ключ носителя (Ed25519, base64 сырых 32 байт).
    #:
    #: Им сервер отзыва проверяет подпись запроса носителя: без неё запрос
    #: получает 403. Закрытая часть живёт только на самом носителе, поэтому
    #: здесь хранится лишь то, что нужно отправить серверу при выпуске и при
    #: повторной публикации записи. Пусто у носителей, записанных до 1.1.0.
    device_public_key_b64: str = ""

    @property
    def service_key(self) -> bytes:
        import base64

        return base64.b64decode(self.service_key_b64) if self.service_key_b64 else b""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MediaRecord":
        """Записи, сделанные прежними версиями, читаются как есть.

        Контейнер администратора переживает обновления программы, и запись,
        созданная до появления нового поля, не должна ронять открытие сеанса:
        неизвестные ключи отбрасываются, отсутствующие берут умолчание.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def display_name(self) -> str:
        """Чем эту строку называть в списке.

        Имя, если администратор его дал; иначе -- модель носителя; в крайнем
        случае идентификатор, который хотя бы уникален.
        """
        return self.name or self.label or self.media_id

    @property
    def is_revoked(self) -> bool:
        return self.status == STATUS_REVOKED

    @property
    def status_text(self) -> str:
        """Состояние строкой для списка -- с датой, потому что без неё
        «отозван» не отличить от «отозван позавчера»."""
        if not self.is_revoked:
            return "действует"
        return f"отозван {self.revoked_at[:10]}" if self.revoked_at else "отозван"


#: Действия, которые записываются в журнал контейнера, и как они называются
#: по-русски. Имя действия хранится ключом, а не готовой фразой: формулировку
#: потом правят, а записанное в контейнере остаётся навсегда.
ACTION_REVOKED = "media_revoked"
ACTION_RESTORED = "media_restored"
ACTION_ERASED = "media_erased"

#: Ресурс отзыва узнал состояние носителя -- или не узнал.
#:
#: Два события, а не одно на двоих: «отозван» в журнале и «ресурс об этом
#: знает» -- разные факты. Через полгода вопрос будет звучать именно так: до
#: носителя дотянулись или только пометили в контейнере.
ACTION_PUBLISHED = "media_state_published"
ACTION_PUBLISH_FAILED = "media_state_not_published"

ACTION_TITLES = {
    ACTION_REVOKED: "Помечен отозванным",
    ACTION_RESTORED: "Отметка отзыва снята",
    ACTION_ERASED: "Заголовки LUKS стёрты",
    ACTION_PUBLISHED: "Состояние принято ресурсом отзыва",
    ACTION_PUBLISH_FAILED: "Состояние до ресурса отзыва не доехало",
}


@dataclass
class JournalEntry:
    """Одно событие в журнале контейнера.

    Отзыв -- решение, а не свойство: его принимают, иногда ошибочно, и потом
    отменяют. Само поле `status` показывает только последнее состояние и не
    отвечает на вопрос, кто и когда носитель отозвал, а потом вернул в строй.
    Журнал отвечает, и живёт он там же, где остальное, -- внутри
    зашифрованного контейнера.
    """

    at: str
    action: str
    media_id: str = ""
    details: str = ""

    @property
    def title(self) -> str:
        return ACTION_TITLES.get(self.action, self.action)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JournalEntry":
        """Те же правила, что у записи носителя: незнакомое отбрасывается.

        Журнал пишет каждая версия программы, а читают его все: событие,
        заведённое версией новее, не должно мешать открыть контейнер в той,
        которая о нём не знает.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class RecoveryRecord:
    media_id: str
    slot: int
    #: Сам код здесь больше не хранится: он выводится из секрета
    #: администратора в момент выдачи. Поле осталось пустым ради контейнеров,
    #: созданных до 29.08.2026, -- в них коды записаны, и выдавать их надо
    #: по-прежнему из записи.
    code: str
    salt_b64: str
    status: str = "available"  # "available" | "used"
    created_at: str = field(default_factory=_now_iso)
    used_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RecoveryRecord":
        return cls(**data)


@dataclass
class ConfigRecord:
    """Конфигурация установки, хранимая внутри контейнера.

    Раньше конфигурации лежали рядом с контейнером обычными `*.json`, и это
    расходилось с замыслом: контейнер задуман как единственное, что
    администратор носит с собой и защищает паролем. Паролей в конфигурации
    нет, но есть полное описание защищаемой системы -- имя учётной записи,
    состав пакетов, что в киоске разрешено и что запрещено, разметка, число
    машин на носитель, адреса зеркал. Карта того, как устроен носитель,
    лежала открытым текстом.

    Хранится словарём, а не разобранным `InstallConfig`: контейнер не должен
    знать схему конфигурации, иначе его формат придётся менять при каждом
    новом поле. Разбирает и собирает `ConfigManager`.
    """

    name: str
    #: Содержимое конфигурации -- то же, что раньше лежало в файле.
    data: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "data": self.data,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConfigRecord":
        return cls(
            name=data.get("name", ""),
            data=data.get("data", {}) or {},
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", data.get("created_at", _now_iso())),
        )


@dataclass
class AdminContainerData:
    media_records: list[MediaRecord] = field(default_factory=list)
    recovery_records: list[RecoveryRecord] = field(default_factory=list)
    #: Конфигурации установки. Живут здесь, а не файлами рядом: контейнер --
    #: единственное, что администратор хранит и защищает паролем.
    configs: list[ConfigRecord] = field(default_factory=list)
    #: Журнал решений администратора о носителях: отзыв, возврат, стирание.
    journal: list[JournalEntry] = field(default_factory=list)
    #: Секрет администратора, из которого выводятся все коды восстановления.
    #:
    #: Он и есть содержимое контейнера, ради которого тот зашифрован. Коды не
    #: хранятся: каждый вычисляется в момент звонка как
    #: `KDF(секрет ‖ media_id ‖ слот)`. Разница не в объёме, а в том, сколько
    #: раз секретное пересекает границу приложения: хранимый код уходит наружу
    #: при каждой выдаче и показывался на экране при развёртывании, а секрет
    #: не покидает контейнер ни разу за всю жизнь носителя.
    #:
    #: Закреплён за администратором и не меняется: слоты на выданных носителях
    #: записаны его производными, и смена секрета означала бы пересборку всех
    #: носителей организации. Забыл пароль от контейнера — контейнер бесполезен,
    #: и это принятое решение, а не недоработка.
    admin_secret_b64: str = ""
    #: Отдельное семя для подписи ответов об отзыве.
    #:
    #: Не то же, что `admin_secret_b64`, и это принципиально: закрытая часть
    #: ключа отзыва уезжает на общедоступный сервер, который однажды взломают.
    #: Взлом такого сервера позволит отзывать носители -- то есть устроить
    #: отказ в обслуживании, -- но не подделать обмен конфигурациями между
    #: администраторами и не вычислить коды восстановления.
    #: Адрес ресурса отзыва и его открытый ключ -- то, что приложение узнало у
    #: сервера при знакомстве.
    #:
    #: Ключ рождается на сервере, а не здесь: сервер разворачивают первым,
    #: когда приложения ещё нет. Носители носят в себе эту открытую часть и по
    #: ней проверяют подпись ответа «отозван», поэтому она хранится в
    #: контейнере: адрес можно спросить заново, а ключ должен быть один и тот
    #: же у всех выпущенных носителей.
    revocation_server_url: str = ""
    revocation_public_key_pem: str = ""
    #: Семя ключа, которым администратор подписывает команды ресурсу отзыва.
    #:
    #: Третий ключ, и снова отдельный. Ключ отзыва уезжает на сервер целиком --
    #: иначе тот не сможет подписывать ответы носителям. Этот не уезжает
    #: никогда: на сервере лежит только его открытая часть, и она позволяет
    #: серверу проверить, что список состояний прислал администратор. Значит
    #: администратору не нужен ни доступ к оболочке сервера, ни токен, который
    #: можно украсть и предъявить где-то ещё, а взломанный сервер не может
    #: выдать себя за администратора.
    admin_authority_secret_b64: str = ""
    #: Семя ключей обмена конфигурациями между администраторами.
    #:
    #: Отдельно от секрета восстановления, хотя оба -- случайные 32 байта.
    #: Тот выводит коды, которые администратор диктует пользователю; это --
    #: ключи, которыми чужие администраторы шифруют присланное. Смешать их
    #: значило бы связать две независимые роли одного контейнера: смена одной
    #: потянула бы за собой другую.
    exchange_seed_b64: str = ""

    def to_dict(self) -> dict:
        return {
            "media_records": [m.to_dict() for m in self.media_records],
            "recovery_records": [r.to_dict() for r in self.recovery_records],
            "configs": [c.to_dict() for c in self.configs],
            "journal": [e.to_dict() for e in self.journal],
            "admin_secret_b64": self.admin_secret_b64,
            "revocation_server_url": self.revocation_server_url,
            "revocation_public_key_pem": self.revocation_public_key_pem,
            "admin_authority_secret_b64": self.admin_authority_secret_b64,
            "exchange_seed_b64": self.exchange_seed_b64,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AdminContainerData":
        return cls(
            media_records=[MediaRecord.from_dict(m) for m in data.get("media_records", [])],
            recovery_records=[RecoveryRecord.from_dict(r) for r in data.get("recovery_records", [])],
            # Контейнер, созданный до 09.09.2026, конфигураций не содержит --
            # пустой список здесь и означает «их ещё не было».
            configs=[ConfigRecord.from_dict(c) for c in data.get("configs", [])],
            # Контейнер, созданный до 11.09.2026, журнала не содержит -- и
            # пустой список здесь означает «событий ещё не записывали», а не
            # «их не было».
            journal=[JournalEntry.from_dict(e) for e in data.get("journal", [])],
            admin_secret_b64=data.get("admin_secret_b64", ""),
            revocation_server_url=data.get("revocation_server_url", ""),
            revocation_public_key_pem=data.get("revocation_public_key_pem", ""),
            admin_authority_secret_b64=data.get("admin_authority_secret_b64", ""),
            exchange_seed_b64=data.get("exchange_seed_b64", ""),
        )
