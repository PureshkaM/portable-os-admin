import base64

import pytest

from portable_os_admin import crypto
from portable_os_admin.container import ContainerManager, MediaRecord, RecoveryRecord
from portable_os_admin.errors import MediaNotRegisteredError, RecoveryDeniedError, RecoveryRequestInvalidError
from portable_os_admin.recovery import RecoveryRequest, RecoveryService
from portable_os_admin.recovery.request import KIND_MACHINES, KIND_PASSWORD
from portable_os_admin.session import AdminSession


@pytest.fixture
def session_with_media(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    session = AdminSession(container)
    media_id = crypto.new_media_id()
    container.add_media_record(
        MediaRecord(media_id=media_id, luks_uuid="u-1", label="stick", config_name="cfg", target_os="debian")
    )
    code = crypto.generate_recovery_code()
    salt = crypto.new_salt()
    container.add_recovery_record(
        RecoveryRecord(media_id=media_id, slot=7, code=code, salt_b64=base64.b64encode(salt).decode())
    )
    return session, media_id, code


def test_request_encode_parse_round_trip():
    request = RecoveryRequest(media_id="abcd1234", slot=3)
    wire = request.encode()
    parsed = RecoveryRequest.parse(wire)
    assert parsed == request


def test_request_parse_rejects_bad_checksum():
    request = RecoveryRequest(media_id="abcd1234", slot=3)
    wire = request.encode()
    tampered = wire[:-1] + ("2" if wire[-1] != "2" else "3")
    with pytest.raises(RecoveryRequestInvalidError):
        RecoveryRequest.parse(tampered)


def test_request_is_short_enough_to_read_over_the_phone():
    """Три группы по четыре знака, без похожих на слух букв.

    Прежний вид -- `66640908:3:a1b2c3d4` -- был вдвое длиннее и показывал
    человеку номер слота, который ему знать незачем. Диктовать такое дольше, а
    ошибиться проще: `0` и `O`, `1` и `I` на слух не различаются, и опечатка
    всплывала только после того, как argon2id отработает восемь секунд.
    """
    wire = RecoveryRequest(media_id="abcd1234", slot=3).encode()

    groups = wire.split("-")
    assert len(groups) == 3
    assert all(len(group) == 4 for group in groups)
    # Номер слота внутри строки, а не отдельным полем: пользователь про слоты
    # не знает и знать не должен.
    assert ":" not in wire

    # Проверяется алфавит целиком, а не одна удачная строка: букв, неотличимых
    # от цифр, в нём нет вовсе, и выпасть они не могут ни на каком носителе.
    from portable_os_admin.recovery.request import _ALPHABET

    assert len(_ALPHABET) == 32
    assert not set(_ALPHABET) & set("ILOU")


def test_a_lookalike_letter_is_understood_rather_than_refused():
    """Написавший «O» вместо нуля не должен получать отказ.

    Строку диктуют по телефону и переписывают с экрана на бумажку. Crockford
    base32 для того и придуман: `O` -- это ноль, `I` и `L` -- единица. Отказ
    на верном по сути коде обошёлся бы пользователю ещё одним звонком
    администратору.
    """
    request = RecoveryRequest(media_id="10a0b1c1", slot=1, kind=KIND_MACHINES)
    wire = request.encode()

    misread = wire.replace("0", "O").replace("1", "I")
    assert RecoveryRequest.parse(misread) == request


def test_request_carries_what_kind_of_trouble_it_is():
    """Забытый пароль и исчерпанный носитель -- разные события.

    В прежнем формате они выглядели одинаково, и администратор не мог понять,
    что у него просят, а носитель -- что делать после того, как код принят.
    """
    for kind in (KIND_PASSWORD, KIND_MACHINES):
        request = RecoveryRequest(media_id="abcd1234", slot=7, kind=kind)
        assert RecoveryRequest.parse(request.encode()).kind == kind

    # И вид меняет саму строку -- иначе он бы никуда не передавался.
    forgot = RecoveryRequest(media_id="abcd1234", slot=7, kind=KIND_PASSWORD).encode()
    exhausted = RecoveryRequest(media_id="abcd1234", slot=7, kind=KIND_MACHINES).encode()
    assert forgot != exhausted


def test_request_parse_forgives_how_a_human_retypes_it():
    """Пробелы, пропущенные дефисы и регистр -- не ошибка пользователя.

    Строку переписывают с экрана на бумажку и обратно; отвергать за нижний
    регистр значило бы придираться, а не проверять.
    """
    request = RecoveryRequest(media_id="abcd1234", slot=3, kind=KIND_MACHINES)
    wire = request.encode()

    for retyped in (wire.lower(), wire.replace("-", ""), wire.replace("-", " ")):
        assert RecoveryRequest.parse(retyped) == request


def test_the_old_format_is_still_understood():
    """Носители с прежним форматом уже собраны и придут за восстановлением."""
    legacy = "abcd1234:3:" + RecoveryRequest(media_id="abcd1234", slot=3).checksum()
    parsed = RecoveryRequest.parse(legacy)

    assert parsed.media_id == "abcd1234"
    assert parsed.slot == 3
    assert parsed.kind == KIND_PASSWORD


@pytest.mark.parametrize("bad_text", ["not-a-request", "a:b:c:d", "media:notanumber:00000000"])
def test_request_parse_rejects_malformed_text(bad_text):
    with pytest.raises(RecoveryRequestInvalidError):
        RecoveryRequest.parse(bad_text)


def test_codes_are_derived_from_the_secret_and_never_stored():
    """Код вычисляется в момент выдачи, а не лежит в контейнере готовым.

    Стойкость от этого не меняется -- меняется, сколько раз секретное
    пересекает границу приложения. Хранимый код уходил наружу при каждой
    выдаче и, хуже того, показывался на экране сразу после развёртывания с
    припиской «запишите его»: содержимое контейнера штатно выносилось наружу
    на каждом собранном носителе. Секрет не покидает контейнер ни разу.
    """
    secret = crypto.new_admin_secret()

    code = crypto.derive_recovery_code(secret, "abcd1234", 3)
    # Вывод детерминирован: администратор получит ту же строку через год.
    assert code == crypto.derive_recovery_code(secret, "abcd1234", 3)
    # Тот же вид, что и раньше: четыре группы по четыре знака.
    assert len(code.split("-")) == 4

    # Носители не делят кодов: media_id входит в вывод.
    assert code != crypto.derive_recovery_code(secret, "abcd1235", 3)
    # И слоты одного носителя тоже.
    assert code != crypto.derive_recovery_code(secret, "abcd1234", 4)
    # Другой администратор -- другие коды на том же носителе.
    assert code != crypto.derive_recovery_code(crypto.new_admin_secret(), "abcd1234", 3)


def test_a_container_created_before_the_change_still_hands_out_its_codes(tmp_path):
    """Носители, выданные раньше, приходят за восстановлением ещё долго.

    Их keyslot'ы записаны случайными кодами, которые лежат в записях. Выдать
    вместо них выведенные значило бы сказать пользователю строку, которая к
    его носителю не подходит.
    """
    cm = ContainerManager()
    container = cm.create(tmp_path / "old.container", "CorrectHorse123")
    session = AdminSession(container)
    media_id = crypto.new_media_id()
    container.add_media_record(
        MediaRecord(media_id=media_id, luks_uuid="u-1", label="s", config_name="c", target_os="redos")
    )
    stored = crypto.generate_recovery_code()
    container.add_recovery_record(
        RecoveryRecord(
            media_id=media_id, slot=7, code=stored,
            salt_b64=base64.b64encode(crypto.new_salt()).decode(),
        )
    )

    issued = RecoveryService(session).issue_code(
        RecoveryRequest(media_id=media_id, slot=7).encode()
    )
    assert issued == stored


def test_issue_code_success(session_with_media):
    session, media_id, code = session_with_media
    request = RecoveryRequest(media_id=media_id, slot=7).encode()
    issued = RecoveryService(session).issue_code(request)
    assert issued == code
    record = session.container.find_recovery(media_id, 7)
    assert record.status == "used"
    assert record.used_at is not None


def test_issue_code_twice_is_denied(session_with_media):
    session, media_id, _ = session_with_media
    request = RecoveryRequest(media_id=media_id, slot=7).encode()
    service = RecoveryService(session)
    service.issue_code(request)
    with pytest.raises(RecoveryDeniedError):
        service.issue_code(request)


def test_issue_code_unknown_media(session_with_media):
    session, _, _ = session_with_media
    # Валидный идентификатор, которого просто нет в контейнере: формат
    # запроса вмещает восемь шестнадцатеричных знаков, и «unknownid» им не
    # является -- это уже другая ошибка, не та, что проверяется здесь.
    request = RecoveryRequest(media_id="deadbeef", slot=7).encode()
    with pytest.raises(MediaNotRegisteredError):
        RecoveryService(session).issue_code(request)


def test_issue_code_unknown_slot(session_with_media):
    session, media_id, _ = session_with_media
    # Слот из заголовка LUKS2, но не заведённый на этом носителе.
    request = RecoveryRequest(media_id=media_id, slot=31).encode()
    with pytest.raises(RecoveryDeniedError):
        RecoveryService(session).issue_code(request)
