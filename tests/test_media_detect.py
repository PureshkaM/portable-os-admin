"""Носитель узнаётся в порту, не спрашивая пароля."""
from __future__ import annotations

from portable_os_admin.admin.media_detect import detect, detect_map
from portable_os_admin.container import MediaRecord
from portable_os_admin.install.devices import BlockDevice, Partition


class FakeLister:
    """Подставной опрос устройств: тесты идут и там, где нет ни одной флешки."""

    def __init__(self, devices):
        self._devices = devices

    def list_all(self):
        return self._devices


def make_device(path: str, uuids: "list[str]", model: str = "Kingston DT") -> BlockDevice:
    return BlockDevice(
        name=path.rsplit("/", 1)[-1],
        path=path,
        size_bytes=16 * 1024**3,
        model=model,
        removable=True,
        mountpoints=(),
        partitions=tuple(
            Partition(
                path=f"{path}{index + 1}",
                size_bytes=1024**3,
                fstype="crypto_LUKS",
                label="",
                mountpoint="",
                uuid=uuid,
            )
            for index, uuid in enumerate(uuids)
        ),
    )


def make_record(media_id: str, luks_uuid: str, **kwargs) -> MediaRecord:
    return MediaRecord(
        media_id=media_id,
        luks_uuid=luks_uuid,
        label="/dev/sdb",
        config_name="kiosk",
        target_os="debian",
        **kwargs,
    )


def test_a_medium_from_this_container_is_recognised_in_the_port():
    """Совпадение UUID означает: этот носитель выдан из этого контейнера.

    Читается это без ключа -- заголовок LUKS для того и открыт, чтобы система
    знала, что перед ней, -- и без монтирования.
    """
    record = make_record("46aed677", "523e0993-4dac-4eee-9f72-a272ce456d34")
    devices = [make_device("/dev/sdb", ["523e0993-4dac-4eee-9f72-a272ce456d34"])]

    found = detect([record], FakeLister(devices))

    assert len(found) == 1
    assert found[0].record.media_id == "46aed677"
    assert found[0].device_path == "/dev/sdb"
    assert found[0].matched_volume == "системный"


def test_a_stranger_stick_is_not_claimed_as_ours():
    """Чужая флешка в том же порту не должна выдаваться за свою."""
    record = make_record("46aed677", "523e0993-4dac-4eee-9f72-a272ce456d34")
    devices = [make_device("/dev/sdb", ["ffffffff-0000-0000-0000-000000000000"])]

    assert detect([record], FakeLister(devices)) == []


def test_recognition_works_by_any_of_the_three_volumes():
    """Системный том мог быть перезаписан, а служебные -- остаться.

    Носитель при этом всё ещё тот же, и сказать об этом полезнее, чем
    промолчать: администратор увидит, каким именно томом он узнан.
    """
    record = make_record(
        "218d2d33",
        "aaaaaaaa-0000-0000-0000-000000000000",
        logs_luks_uuid="bbbbbbbb-0000-0000-0000-000000000000",
        exchange_luks_uuid="cccccccc-0000-0000-0000-000000000000",
    )
    devices = [make_device("/dev/sdc", ["cccccccc-0000-0000-0000-000000000000"])]

    found = detect([record], FakeLister(devices))

    assert len(found) == 1
    assert found[0].matched_volume == "обмена"


def test_a_rewritten_medium_stops_being_listed_as_plugged_in():
    """В одном порту -- один носитель, а не все, что там когда-то были.

    Замечено 14.09.2026: список предлагал три носителя, и все с пометкой
    `/dev/loop1`, хотя перезаписаны были первые два. Выбрать из них было
    нечего: приложение честно отвечало «носитель с томом ... не найден», но
    только после выбора.
    """
    old = make_record("aaaaaaaa", "11111111-0000-0000-0000-000000000000")
    older = make_record(
        "bbbbbbbb",
        "22222222-0000-0000-0000-000000000000",
        logs_luks_uuid="33333333-0000-0000-0000-000000000000",
    )
    current = make_record("cccccccc", "44444444-0000-0000-0000-000000000000")
    # На устройстве сейчас тома последней записи -- и, скажем, уцелевший том
    # журналов предыдущей: разделы перезаписываются не всегда все сразу.
    devices = [
        make_device(
            "/dev/loop1",
            ["44444444-0000-0000-0000-000000000000", "33333333-0000-0000-0000-000000000000"],
        )
    ]

    found = detect([old, older, current], FakeLister(devices))

    assert [item.record.media_id for item in found] == ["cccccccc"]
    assert found[0].matched_volume == "системный"


def test_the_later_record_wins_when_both_match_the_same_way():
    """Записи идут в порядке выдачи: последняя описывает то, что в порту."""
    first = make_record("aaaaaaaa", "11111111-0000-0000-0000-000000000000")
    second = make_record("bbbbbbbb", "11111111-0000-0000-0000-000000000000")
    devices = [make_device("/dev/sdb", ["11111111-0000-0000-0000-000000000000"])]

    found = detect([first, second], FakeLister(devices))

    assert [item.record.media_id for item in found] == ["bbbbbbbb"]


def test_uuid_case_does_not_matter():
    """blkid печатает строчными, заголовок хранит как записали."""
    record = make_record("46aed677", "523E0993-4DAC-4EEE-9F72-A272CE456D34")
    devices = [make_device("/dev/sdb", ["523e0993-4dac-4eee-9f72-a272ce456d34"])]

    assert len(detect([record], FakeLister(devices))) == 1


def test_a_failing_device_scan_does_not_break_the_screen():
    """Определение носителя -- подсказка, а не обязанность.

    Экран списка носителей должен открываться и на машине, где опрос
    устройств почему-либо не работает.
    """

    class Broken:
        def list_all(self):
            raise OSError("lsblk недоступен")

    record = make_record("46aed677", "523e0993-4dac-4eee-9f72-a272ce456d34")
    assert detect([record], Broken()) == []


def test_records_keep_their_order_whatever_is_plugged_in():
    """Список на экране не должен перестраиваться от подключения флешки."""
    first = make_record("aaa", "11111111-0000-0000-0000-000000000000")
    second = make_record("bbb", "22222222-0000-0000-0000-000000000000")
    devices = [
        make_device("/dev/sdc", ["22222222-0000-0000-0000-000000000000"]),
        make_device("/dev/sdb", ["11111111-0000-0000-0000-000000000000"]),
    ]

    found = detect([first, second], FakeLister(devices))

    assert [item.record.media_id for item in found] == ["aaa", "bbb"]
    assert set(detect_map([first, second], FakeLister(devices))) == {"aaa", "bbb"}


def test_the_display_name_prefers_what_the_administrator_typed():
    """Идентификатор отвечает «тот ли это носитель», имя -- «чей он»."""
    record = make_record("46aed677", "u")
    assert record.display_name == "/dev/sdb"  # пока имени нет -- метка системы

    record.name = "Иванов, бухгалтерия"
    assert record.display_name == "Иванов, бухгалтерия"


def test_the_deploy_wizard_asks_who_the_medium_is_for():
    """Имя и файлы спрашиваются при записи, а не после неё.

    После записи носитель попадает в список, где строки различаются
    идентификаторами вроде `46aed677`; назвать его постфактум означает искать
    нужную строку среди одинаковых. А раздел обмена в момент записи уже
    смонтирован -- положить в него файл здесь ничего не стоит, тогда как
    потом это отдельный заход: открыть контейнер, подключить носитель,
    открыть том, скопировать.
    """
    from portable_os_admin.gui.screens.deploy_wizard import DeployWizardFrame

    keys = DeployWizardFrame._STEP_KEYS
    assert "handover" in keys
    # Перед подтверждением, а не после: на последнем шаге уже идёт запись.
    assert keys.index("handover") < keys.index("confirm")

    assert hasattr(DeployWizardFrame, "_save_step_handover")
    assert hasattr(DeployWizardFrame, "_validate_step_handover")

    # Каждый ключ обязан находить свой строитель.
    #
    # Пока шаги были списком ссылок на функции, рассогласование «в списке есть,
    # а метода нет» было невозможно. Теперь раздача ищет метод по имени ключа,
    # и опечатка в ключе даёт мастер, падающий на этом шаге у администратора, а
    # не здесь. Поэтому проверяется весь набор, а не один шаг.
    missing = [key for key in keys if not hasattr(DeployWizardFrame, f"_build_step_{key}")]
    assert not missing, f"ключи без строителя: {missing}"


def test_the_installer_accepts_a_name_and_files_for_the_exchange():
    """Установщик принимает и то, и другое, и ничего не требует.

    Значения по умолчанию пустые: развёртывание без имени и без файлов -- это
    обычный случай, а не забытый аргумент.
    """
    import inspect

    from portable_os_admin.install.installer import OSInstaller

    signature = inspect.signature(OSInstaller.deploy_distribution)
    assert signature.parameters["media_name"].default == ""
    assert signature.parameters["exchange_files"].default == ()


def test_a_failed_copy_into_the_exchange_does_not_lose_the_medium():
    """Носитель, записанный за час, не выбрасывают из-за файла настроек.

    Ошибка копирования возвращается предупреждением: носитель годен, а файл
    кладётся потом через раздел «Обмен».
    """
    from portable_os_admin.install.installer import OSInstaller

    class Layout:
        mount_root = "/mnt/target"

    def failing_run(argv, **kwargs):
        raise OSError("нет места")

    installer = OSInstaller.__new__(OSInstaller)
    problems = installer._fill_exchange(failing_run, Layout(), ["/tmp/settings.ini"])

    assert len(problems) == 1
    assert "раздел обмена" in problems[0]
    assert "Обмен" in problems[0], "сказано, где положить файл потом"


def test_nothing_is_copied_when_nothing_was_chosen():
    from portable_os_admin.install.installer import OSInstaller

    calls = []
    installer = OSInstaller.__new__(OSInstaller)
    assert installer._fill_exchange(lambda argv, **kw: calls.append(argv), object(), []) == []
    assert calls == []


def test_both_ways_of_writing_a_medium_go_through_one_helper():
    """Развёртывание и установка кладут на носитель один и тот же набор.

    Раньше этот тест сторожил другое: раздел обмена монтировался в двух местах
    кода, и рядом с каждым монтированием обязан был стоять вызов, который его
    наполняет. Так и было найдено 04.09.2026 -- правка стояла только в пути
    установки с образа, носитель развернулся успешно, а каталог настроек в
    разделе обмена не появился вовсе.

    Два места и были корнем беды: за два дня разъезд путей стоил четырёх
    дефектов, каждый виден только живой загрузкой. Теперь путь один --
    `_write_medium_files`, -- и сторожить надо именно это: новая вещь на
    носителе физически не может попасть лишь в одну ветку.
    """
    import inspect

    from portable_os_admin.install import installer as installer_module

    source = inspect.getsource(installer_module)
    mounts = source.count('run(["mount", layout.exchange_mapper_path')
    assert mounts == 1, "раздел обмена монтируется больше чем в одном месте -- пути разъехались"

    assert "def _write_medium_files(" in source, "общего помощника нет"
    calls = source.count("self._write_medium_files(")
    assert calls == 2, "помощник вызван не из обоих путей записи носителя"

    installer_source = inspect.getsource(installer_module.OSInstaller._write_medium_files)
    for needed in ("config_files", "scripted_files", "_write_revocation_key", "_fill_exchange"):
        assert needed in installer_source, f"общий помощник не пишет {needed}"
