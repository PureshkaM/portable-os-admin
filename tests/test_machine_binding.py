"""Привязка носителя к машинам.

Тесты проверяют форму того, что записывается, — большего они здесь и не могут:
подойдёт ли ключ на живой машине, соблюдёт ли dracut порядок хуков и спросит
ли cryptsetup пароль ровно один раз, выясняется только загрузкой. Это
записано в README, раздел «Проверки, стенд и уроки» как непроверенное.

Что они ловят — ошибку, которой этот механизм подвержен больше всего:
сгенерированный, но неработающий скрипт. Ошибка синтаксиса в keyscript или
пустая подстановка означают носитель, который не открывается вовсе.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from portable_os_admin.config import InstallConfig
from portable_os_admin.install import machine_binding as mb
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.disk import build_media_layout, plan_partitions


def make_config(target_os: str = "debian", slots: int = 5) -> InstallConfig:
    config = InstallConfig.create_default("stick", target_os)
    config.machine_binding.slots = slots
    return config


def layout_for(config: InstallConfig):
    layout = build_media_layout(
        plan_partitions("/dev/sdz", config.disk),
        mapper_name="poa-abc12345",
        mount_root="/mnt/t",
    )
    layout.luks_uuid = "11111111-2222-3333-4444-555555555555"
    return layout


def shell_is_valid(text: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(text)
        path = handle.name
    try:
        return subprocess.run(["sh", "-n", path], capture_output=True).returncode == 0
    finally:
        os.unlink(path)


# ----------------------------------------------------------------------
# Раскладка слотов
# ----------------------------------------------------------------------
def test_recovery_gets_every_slot_the_machines_did_not_take():
    """Чем меньше машин разрешил администратор, тем больше кодов помещается.

    Код расходуется безвозвратно и тратится на два события: забытый пароль и
    очистку списка машин. Остаток заголовка отдаётся под них целиком.
    """
    few, many = make_config(slots=3), make_config(slots=7)

    assert len(mb.machine_keyslots(few)) == 3
    assert len(mb.recovery_keyslots(few)) == 28
    assert len(mb.recovery_keyslots(many)) == 24
    # Слоты не пересекаются: один keyslot -- одно назначение.
    assert not set(mb.machine_keyslots(few)) & set(mb.recovery_keyslots(few))


def test_switching_the_binding_off_leaves_the_header_to_recovery():
    off = make_config(slots=0)

    assert mb.machine_keyslots(off) == ()
    assert len(mb.recovery_keyslots(off)) == 31
    assert not mb.is_enabled(off)


# ----------------------------------------------------------------------
# Скрипты
# ----------------------------------------------------------------------
def test_generated_scripts_are_valid_shell():
    """Ошибка синтаксиса здесь -- это носитель, который не открывается."""
    config = make_config("redos")

    assert shell_is_valid(mb.key_script(config))
    assert shell_is_valid(mb.register_script(config, "abc12345"))
    assert shell_is_valid(mb.dracut_unlock_script(config, "poa-abc12345"))


def test_nothing_is_generated_when_the_binding_is_off():
    off = make_config(slots=0)

    assert mb.key_script(off) == ""
    assert mb.register_script(off, "abc") == ""
    assert mb.dracut_unlock_script(off, "poa-abc") == ""


def test_the_key_is_derived_from_the_passphrase_and_the_fingerprint():
    script = mb.key_script(make_config())

    assert "poa_fingerprint_id" in script
    assert "sha256sum" in script
    # Отпечаток -- плата и процессор. Сетевые адреса не входят: док-станция
    # меняла бы отпечаток, и одна машина занимала бы слот за слотом.
    assert "/sys/class/dmi/id/board_serial" in script
    assert "model name" in script
    for absent in ("/sys/class/net", "macaddress", "ip link"):
        assert absent not in script


def test_the_registration_gives_up_the_machine_rather_than_the_limit():
    """Отказался вносить машину -- система не запускается.

    Смысл механизма в том, что на незарегистрированной машине носитель не
    работает; «продолжить без записи» противоречило бы этому.
    """
    script = mb.register_script(make_config(), "abc12345")

    assert "poweroff -f" in script
    # Весь смысл -- в одной строке вопроса, а не в россыпи сообщений: тема
    # plymouth их не центрирует и не убирает, и на экране 29.08.2026 от
    # объяснения остались обрывки в левом верхнем углу.
    question = next(line for line in script.splitlines() if line.startswith("question="))
    # Пользователю сказано, что место занимается навсегда: согласие даётся
    # один раз, а освободить его сможет только администратор.
    assert "Освободить сможет только администратор" in question
    # Видно, сколько мест уходит и сколько всего.
    assert "$SLOTS" in question
    # И ответить можно с той раскладки, которая есть. Консоль загрузки
    # латинская, кириллическое «да» набрать физически нечем: на прогоне
    # 28.08.2026 вопрос «(да/нет)» удалось подтвердить лишь латинским `y`,
    # которого в самом вопросе не было.
    assert "yes/no" in question
    # Последний слот забирает с собой регистрационный ключ -- именно это
    # превращает механизм из проверки в гарантию.
    assert "luksKillSlot" in script


# ----------------------------------------------------------------------
# Как это попадает на носитель
# ----------------------------------------------------------------------
def test_debian_asks_cryptsetup_to_call_the_keyscript():
    """У Debian это штатный механизм: update-initramfs сам копирует скрипт."""
    config = make_config("debian")
    crypttab = get_backend("debian").config_files(config, layout_for(config))["etc/crypttab"]

    root_row = next(line for line in crypttab.splitlines() if line.startswith("poa-abc12345"))
    assert f"keyscript=/{mb.KEY_SCRIPT_PATH}" in root_row


def test_redos_crypttab_carries_no_keyscript():
    """`keyscript=` -- интерфейс cryptsetup-initramfs, то есть Debian.

    В RHEL-семействе том открывает хук dracut, а systemd-cryptsetup такой
    опции не знает. crypttab при этом один на оба семейства, поэтому опция,
    написанная для одного, молча уезжала на носитель другого -- тот самый
    класс ошибки, ради которого киоск разнесли по средам 27.08.2026.

    Найдено ревизией собранного носителя 28.08.2026, а не прогоном: дефекты
    этого дня сидели на границах этапов загрузки, и искать их оказалось
    дешевле разбором цепочки, чем очередной загрузкой.
    """
    config = make_config("redos")
    crypttab = get_backend("redos").config_files(config, layout_for(config))["etc/crypttab"]

    root_row = next(line for line in crypttab.splitlines() if line.startswith("poa-abc12345"))
    assert "keyscript" not in root_row
    # Остальное на месте: том по-прежнему описан, и лимит попыток тоже.
    assert "luks,discard" in root_row


def test_redos_opens_the_volume_itself_instead_of_systemd_cryptsetup():
    """С rd.luks.* systemd-cryptsetup спросил бы пароль раньше любого хука.

    Перехватить его порядком хуков нельзя: dracut документирует, что в
    systemd-режиме приоритеты хуков не соблюдаются.
    """
    config = make_config("redos")
    layout = layout_for(config)
    backend = get_backend("redos")

    assert backend.unlock_kernel_parameters(layout, config) == []
    config.machine_binding.slots = 0
    assert backend.unlock_kernel_parameters(layout, config) != []


def test_the_machine_key_is_tried_only_against_machine_slots():
    """Проверка ключа машины идёт по её слотам поимённо, а не по заголовку.

    Без `--key-slot` cryptsetup перебирает каждый занятый keyslot. На готовом
    носителе их тридцать, и каждый -- отдельный argon2id на 256 МиБ: 8,4
    секунды по замерам этого проекта, то есть больше четырёх минут ожидания
    перед вводом пароля на живой машине. Под полной эмуляцией счёт идёт на
    часы -- прогон 28.08.2026 на этом и встал, а выглядело это как намертво
    зависшая загрузка, по которой причину не угадать.

    При двух разрешённых машинах проверок должно быть ровно две.
    """
    config = make_config("redos")
    config.machine_binding.slots = 2
    script = mb.dracut_unlock_script(config, "poa-abc")

    assert "--key-slot" in script, "перебирается весь заголовок вместо машинных слотов"
    assert "POA_FIRST_MACHINE_SLOT=1" in script
    assert "POA_LAST_MACHINE_SLOT=2" in script

    # Границы следуют за числом машин, а не зашиты.
    config.machine_binding.slots = 5
    wider = mb.dracut_unlock_script(config, "poa-abc")
    assert "POA_LAST_MACHINE_SLOT=5" in wider

    # То же и в регистрации: «эта машина уже зарегистрирована» проверяется на
    # КАЖДОЙ загрузке знакомой машины, то есть в самом частом случае из всех.
    config.machine_binding.slots = 2
    register = mb.register_script(config, "abcd1234")
    assert "--key-slot" in register, "регистрация перебирает весь заголовок"

    # И само открытие тома тоже идёт по известному слоту. Дороже всего это
    # обходилось на отказе: на незарегистрированной машине ключ не подходит
    # никуда, cryptsetup перебирал все тридцать слотов, и так трижды подряд.
    # Замерено 28.08.2026 -- одиннадцать минут на попытку, экран при этом
    # неотличим от зависшего.
    assert 'luksOpen --key-slot "$slot"' in script


def test_an_exhausted_medium_asks_for_the_password_only_once():
    """Три попытки там, где не поможет и тридцать.

    Если регистрационный слот уничтожен, носитель занял все машинные места, и
    на незнакомой машине верная фраза не откроет том никогда: подходящего
    ключа в заголовке не существует. Человеку, которому дальше всё равно
    звонить администратору, предлагалось ввести пароль трижды -- и каждая
    попытка стоила ему минут ожидания. Замечено на прогоне 29.08.2026.
    """
    config = make_config("redos")
    config.machine_binding.slots = 2
    script = mb.dracut_unlock_script(config, "poa-abc")

    assert "poa_registration_slot_alive" in script
    # Выход из цикла попыток, а не продолжение.
    tail = script[script.index("poa_registration_slot_alive; then"):]
    assert tail.split("fi")[0].strip().endswith("break")


def test_the_unlock_script_waits_for_the_medium_to_appear():
    """Накопитель появляется позже, чем udev успокаивается.

    Замерено на стенде 28.08.2026: initqueue завершился на 11,55 секунде, а
    sda обнаружился на 12,5. Хук, выходивший по `[ -b "$device" ] || exit 0`,
    отрабатывал вхолостую -- том оставался закрытым, и systemd потом две с
    половиной минуты ждал корневое устройство, которого без этого хука не
    появится. Молчаливый выход здесь неотличим от «всё в порядке».
    """
    config = make_config("redos")
    config.machine_binding.slots = 2
    script = mb.dracut_unlock_script(config, "poa-abc")

    assert "while [ ! -b \"$device\" ]" in script, "хук не ждёт появления устройства"
    # Ожидание конечное: носитель, у которого раздела нет вовсе, не должен
    # висеть вечно -- дальше очередь модуля восстановления.
    assert "-lt 60" in script


def test_the_dracut_module_unlocks_inside_initqueue_not_after_it():
    """Хук разблокировки обязан успеть до ожидания корня, а не после.

    Прогон 28.08.2026: носитель с привязкой не загрузился вовсе. dracut ждёт
    появления корневого устройства заданием initqueue/finished/devexists-<uuid
    корня>, а корень появляется только после расшифровки; pre-mount же идёт
    ПОСЛЕ initqueue. Хук, который единственный мог открыть том, ждал шага, до
    которого его собственное бездействие не пускало, и через две минуты
    initqueue уходил в dracut-emergency с сообщением про несуществующий UUID.

    Тот же симптом уже ловили 26.08.2026, когда в командную строку не попадал
    rd.luks.uuid. Привязка снимает эти параметры намеренно, поэтому место хука
    здесь -- не деталь оформления, а условие загружаемости.
    """
    config = make_config("redos")
    files = get_backend("redos").recovery_boot_files(layout_for(config), config)

    setup = files["usr/lib/dracut/modules.d/90poa-machine/module-setup.sh"][0]
    # dracut не включает модуль просто потому, что тот лежит рядом.
    assert "add_dracutmodules" in files["etc/dracut.conf.d/90-poa-machine.conf"][0]
    # Сначала попытка открыть, потом разговор о том, что она не удалась --
    # и то, и другое внутри initqueue, пока корень ещё ждут.
    assert "inst_hook initqueue/settled 50" in setup
    assert "inst_hook initqueue/settled 99" in setup
    assert "usr/lib/dracut/modules.d/90poa-machine/poa-machine-rescue.sh" in files
    # Регистрация -- наоборот, строго после того как том открыт.
    assert "inst_hook pre-pivot 50" in setup
    # cryptsetup кладётся явно: модуль crypt в systemd-режиме приносит
    # `systemd-cryptsetup`, а не утилиту, на которую рассчитывают оба модуля.
    # Без него верная фраза трижды подряд «не подходила»: вызовы падали с
    # command not found, а `if cryptsetup ...; else` уходил в else, и ошибка
    # выглядела ровно как неверный пароль.
    assert "inst_multiple cryptsetup" in setup
    recovery_setup = files["usr/lib/dracut/modules.d/91poa-recovery/module-setup.sh"][0]
    assert "inst_multiple cryptsetup" in recovery_setup
    # Модуль восстановления остаётся на pre-mount для носителей без привязки:
    # там том открывает systemd-cryptsetup, и хук, сработавший раньше ввода
    # пароля, спрашивал бы код у того, кто ещё не ошибся ни разу.
    recovery_setup = files["usr/lib/dracut/modules.d/91poa-recovery/module-setup.sh"][0]
    assert "pre-mount 99" in recovery_setup


def test_the_scripts_reach_the_root_with_an_execute_bit():
    """Скрипт, записанный без права на запуск, не выполнится и промолчит."""
    config = make_config("debian")
    files = get_backend("debian").scripted_files(config, layout_for(config))

    assert files[mb.KEY_SCRIPT_PATH][1] == 0o755
    assert files[mb.REGISTER_SCRIPT_PATH][1] == 0o755


def test_the_unlock_script_takes_down_its_own_progress_message():
    """«Проверяю пароль…» снимается, как только пароль проверен.

    plymouth держит сообщение, пока его не уберут. Хук разблокировки после
    успешного открытия тома передаёт экран шагу регистрации, и тот рисует
    свой вопрос в другом месте, — непогашенное «Проверяю пароль…» так и
    висело сверху, поверх вопроса «занять место 1 из 2?», уже неверное:
    пароль к этому моменту проверен. Снято на прогоне сброса слотов
    31.08.2026; тот же класс, что и наложение сообщений 29.08.

    Текст обязан быть один на показ и на снятие: plymouth снимает сообщение
    по совпадению строки, и разойдись они хоть многоточием — hide-message не
    убрал бы ничего, а тест бы этого не заметил.
    """
    config = make_config("redos")
    config.machine_binding.slots = 2
    script = mb.dracut_unlock_script(config, "poa-abc")

    assert script.count('POA_CHECKING_MESSAGE="Проверяю пароль…"') == 1, (
        "текст сообщения задан не в одном месте"
    )
    # Обе команды берут текст из переменной, а не пишут его заново.
    assert 'display-message --text="Проверяю' not in script
    assert 'display-message --text="$POA_CHECKING_MESSAGE"' in script
    assert 'hide-message --text="$POA_CHECKING_MESSAGE"' in script

    # Снимается на обоих исходах проверки: и когда том открылся -- дальше
    # экран достаётся регистрации, -- и когда нет: дальше либо следующая
    # попытка, либо модуль восстановления, и каждый говорит своё.
    opened = script[script.index("cryptsetup luksOpen --key-slot \"$slot\""):]
    assert "poa_checking_hide" in opened.split("exit 0")[0], "не снято перед передачей экрана"
    assert opened.count("poa_checking_hide") >= 2, "не снято на пути отказа"


def test_the_attempt_limit_reaches_the_medium_with_binding():
    """Пять попыток, заданные администратором, должны быть пятью.

    `luks.failed_attempts_limit` доходил только до `tries=` в crypttab, то
    есть до Debian, где повторы делает cryptroot. На РЕД ОС при включённой
    привязке том открывает наш хук, а в нём стояла вписанная в код тройка:
    администратор ставил пять и молча получал три. Поле существовало, но до
    носителя не доезжало -- тот же класс, что `luks.kdf` и
    `firewall.open_ports`, которые в этом проекте уже находили. Найдено
    разбором 01.09.2026.
    """
    config = make_config("redos")
    config.machine_binding.slots = 2
    config.luks.failed_attempts_limit = 5
    script = mb.dracut_unlock_script(config, "poa-abc")

    assert "POA_MAX_ATTEMPTS=5" in script
    assert '-le "$POA_MAX_ATTEMPTS"' in script
    # Тройки, вписанной в код, больше нет ни в каком виде.
    assert '-le 3' not in script

    # И значение действительно берётся из конфигурации, а не совпало случайно.
    config.luks.failed_attempts_limit = 1
    assert "POA_MAX_ATTEMPTS=1" in mb.dracut_unlock_script(config, "poa-abc")
