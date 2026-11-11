"""Воспроизводимый атлас архитектуры. Запуск: python3 tools/diagram/architecture_atlas.py."""
from pathlib import Path
import html
import json
import xml.etree.ElementTree as ET

from drawio_lib import Page, write

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs' / 'architecture'
REVISION = '43faf8a'
DATE = '11.11.2026'
PAGES = []
COLORS = {'code': ('#dae8fc', '#4775a8'), 'partial': ('#fff2cc', '#b8860b'),
          'target': ('#e1d5e7', '#9673a6'), 'external': ('#f5f5f5', '#666666')}


def base(slug, title, note, sources):
    p = Page(title, width=1320, height=960)
    p.vertex(html.escape(title), 'text;html=1;align=left;fontSize=23;fontStyle=1;fontColor=#172b4d;', 40, 15, 1240, 48)
    p.vertex(html.escape(note), 'text;html=1;align=left;whiteSpace=wrap;fontSize=13;fontColor=#344563;', 40, 64, 1240, 55)
    p.vertex('Синий — код · Жёлтый — частично · Фиолетовый пунктир — цель · Серый — внешнее / пояснение',
             'text;html=1;align=left;fontSize=12;fontColor=#555555;', 40, 858, 1240, 30)
    p.vertex(html.escape(f'{DATE} · live-2.0 · исходный код {REVISION} · наличие кода ≠ проверка на стенде'),
             'text;html=1;align=left;fontSize=11;fontColor=#555555;', 40, 892, 1240, 20)
    p.vertex(html.escape('Источники: ' + ' · '.join(sources)),
             'text;html=1;align=left;whiteSpace=wrap;fontSize=10;fontColor=#555555;', 40, 917, 1240, 38)
    for source in sources:
        assert (ROOT / source).is_file(), source
    PAGES.append({'slug': slug, 'title': title, 'note': note, 'sources': sources, 'page': p})
    return p


def graph(slug, title, note, nodes, edges, sources):
    p = base(slug, title, note, sources)
    ids = {}
    positions = {n[0]: (n[1], n[2]) for n in nodes}
    for key, col, row, text, *opts in nodes:
        status = opts[0] if opts else 'code'
        shape = opts[1] if len(opts) > 1 else 'box'
        fill, stroke = COLORS[status]
        style = f'rounded=1;whiteSpace=wrap;html=1;fontSize=12;spacing=5;fillColor={fill};strokeColor={stroke};fontColor=#172b4d;'
        if status == 'target':
            style += 'dashed=1;'
        w, h = 140, 60
        if shape == 'decision':
            style += 'rhombus;rounded=0;'
            h = 80
        if shape == 'db':
            style += 'shape=cylinder3;size=12;'
        ids[key] = p.vertex(html.escape(text).replace('\n', '<br>'), style, 40 + col * 180, 160 + row * 120, w, h)
    for edge in edges:
        a, b, label, *opts = edge
        status = opts[0] if opts else 'code'
        style = 'edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;labelBackgroundColor=#ffffff;endArrow=block;'
        style += 'strokeColor=' + COLORS[status][1] + ';'
        if status == 'target':
            style += 'dashed=1;'
        ac, ar = positions[a]
        bc, br = positions[b]
        if ar == br and abs(ac - bc) > 2:
            style += 'exitX=0.5;exitY=1;entryX=0.5;entryY=1;'
        elif ac == bc and abs(ar - br) > 2:
            style += 'exitX=1;exitY=0.5;entryX=1;entryY=0.5;'
        elif ac != bc and ar != br:
            style += f'exitX=0.5;exitY={1 if br > ar else 0};entryX=0.5;entryY={0 if br > ar else 1};'
        p.edge(ids[a], ids[b], html.escape(label), style)


def sequence(slug, title, note, actors, messages, sources):
    p = base(slug, title, note, sources)
    xs = [100 + i * (1080 / max(1, len(actors) - 1)) for i in range(len(actors))]
    for i, actor in enumerate(actors):
        p.vertex(html.escape(actor), 'rounded=1;html=1;whiteSpace=wrap;fillColor=#dae8fc;strokeColor=#4775a8;fontSize=13;', xs[i] - 65, 145, 130, 55)
        # Линия жизни — геометрия, а не зависимость архитектуры.
        cid = p.nid('life')
        p.cells.append(f'<mxCell id="{cid}" edge="1" parent="1" style="endArrow=none;dashed=1;strokeColor=#aaaaaa;"><mxGeometry relative="1" as="geometry"><mxPoint x="{xs[i]}" y="200" as="sourcePoint"/><mxPoint x="{xs[i]}" y="825" as="targetPoint"/></mxGeometry></mxCell>')
    gap = min(65, 570 / max(1, len(messages)))
    for i, (a, b, text, *opts) in enumerate(messages):
        status = opts[0] if opts else 'code'
        y = 230 + i * gap
        cid = p.nid('msg')
        label = html.escape(f'{i + 1}. {text}', quote=True)
        color = COLORS[status][1]
        dash = 'dashed=1;' if status == 'target' else ''
        p.cells.append(f'<mxCell id="{cid}" value="{label}" edge="1" parent="1" style="html=1;endArrow=block;fontSize=12;labelBackgroundColor=#ffffff;strokeColor={color};{dash}"><mxGeometry relative="1" as="geometry"><mxPoint x="{xs[a]}" y="{y}" as="sourcePoint"/><mxPoint x="{xs[b]}" y="{y}" as="targetPoint"/></mxGeometry></mxCell>')


def build():
    graph('01-context', '01 · Контекст системы «Шлейф»',
          'Удалённая работа остаётся на рабочем месте компании. «Основа» — предложенное название корневой сущности.', [
        ('owner',0,0,'Владелец корня','external'),('root',2,0,'«Основа»\nдоверие организации','partial'),('admin',0,2,'Администратор','external'),
        ('tk',2,2,'Ткач\nвыпуск и управление'),('server',4,2,'Кокон\nдопуск и назначения'),('user',0,4,'Сотрудник','external'),
        ('nit',2,4,'Нить\nпереносная среда'),('vpn',4,4,'VPN-шлюз\nорганизации','external'),('work',6,4,'Рабочее место\nкомпании','external')], [
        ('owner','root','офлайн'),('root','tk','сертификаты'),('admin','tk','управление'),('tk','server','подписанные команды'),('tk','nit','выпуск / сервис'),
        ('user','nit','пароль / TOTP'),('nit','server','запрос допуска'),('nit','vpn','туннель'),('vpn','work','RDP / VNC / SSH')], ['docs/SOLUTION.md','src/portable_os_admin/admin/access_actions.py'])
    graph('02-deployment','02 · Размещение и зоны доверия','Кокон выдаёт допуск; пользовательский трафик идёт через VPN-шлюз, а не через HTTP-сервер Кокона.',[
        ('root',0,0,'Сейф / офлайн\nкорневой носитель','partial'),('tk',0,2,'АРМ администратора\nLinux · Ткач'),('internet',2,2,'Недоверенная сеть\nTLS с закреплением','external'),
        ('proxy',4,1,'DMZ · nginx\nHTTPS / TLS'),('app',6,1,'DMZ · Кокон\nHTTP на loopback'),('db',6,3,'Серверная БД\nPostgreSQL / SQLite','code','db'),
        ('nit',0,4,'Чужой компьютер\nзагрузка Нити'),('vpn',4,4,'DMZ · VPN-шлюз','external'),('work',6,4,'Внутренняя сеть\nрабочие места','external')],
        [('root','tk','подключение'),('tk','internet','администрирование'),('nit','internet','запросы'),('internet','proxy','HTTPS'),('proxy','app','прокси'),('app','db','состояние'),('nit','vpn','VPN'),('vpn','work','доступ к цели')],['packaging/revocation-server/install.sh','docs/SOLUTION.md'])
    graph('03-tkach','03 · Компоненты «Ткача»','Текущая ветка: Tkinter. Перенос на PySide6 находится в отдельной ветке и здесь не показан как готовый.',[
        ('ui',0,0,'gui/\nэкраны и мастера'),('session',2,0,'AdminSession\nоткрытый сеанс'),('container',4,0,'container/ + crypto\nшифрование файла'),
        ('config',0,2,'config/\nконфигурации'),('admin',2,2,'admin/\nоперации устройств'),('recovery',4,2,'recovery/\nодноразовые коды'),
        ('installer',0,4,'install/\nсборка и запись'),('api',2,4,'Клиенты Кокона\nподписи и TLS'),('linux',4,4,'Linux-инструменты\npolkit / LUKS / GRUB','external')],
        [('ui','session','сеанс'),('session','container','данные'),('ui','config','настройка'),('session','admin','полномочия'),('admin','recovery','восстановление'),('config','installer','параметры'),('admin','api','команды'),('installer','linux','привилегии')],['src/portable_os_admin/gui/app.py','src/portable_os_admin/session.py','src/portable_os_admin/install/installer.py'])
    graph('04-cocoon','04 · Компоненты «Кокона»','API — интерфейс обмена программ. Секреты сейчас загружаются из файлов учётных данных службы.',[
        ('http',0,0,'HTTP Handler\nv2 и v3'),('auth',2,0,'Подпись / nonce\nзащита повторов'),('v3',4,0,'poa_access/v3\nдопуск и команды'),
        ('store',2,2,'Store + Database\nсостояние устройств'),('ca',4,2,'CA / CRL\nVPN-сертификаты'),('secret',6,2,'ServerSecrets\nключи службы'),
        ('files',0,4,'Выпуски ОС\nраздача файлов'),('db',2,4,'SQLite / PostgreSQL','code','db'),('log',4,4,'События и\nжурнал сервера')],
        [('http','auth','проверить'),('auth','v3','разрешить'),('v3','store','транзакции'),('v3','ca','сертификат'),('ca','secret','ключ CA'),('v3','secret','TOTP / подпись'),('store','db','запись'),('http','files','раздача выпусков'),('store','log','события')],['packaging/revocation-server/poa-revocation-server.py','packaging/revocation-server/poa_access/v3.py','packaging/revocation-server/poa_access/secrets_store.py'])
    graph('05-nit','05 · Компоненты «Нити»','Служба управляет сетью, обновлениями и событиями. Окно запрашивает действия через локальный сокет.',[
        ('ui',0,0,'poa_connect\nокно подключения'),('service',2,0,'poa_accessd\nслужба root'),('server',4,0,'Кокон','external'),
        ('client',0,2,'RDP / VNC / SSH\nудалённый сеанс'),('vpn',2,2,'OpenVPN\nвременный сеанс'),('firewall',4,2,'nftables\nэтапы допуска'),
        ('update',0,4,'poa_update\nобновление A/B'),('trust',2,4,'poa_trust\nподписи / якоря'),('events',4,4,'poa_events\nочередь событий')],
        [('ui','service','локальный сокет'),('service','server','HTTPS'),('ui','client','запуск'),('service','vpn','поднять / остановить'),('service','firewall','разрешённые адреса'),('update','trust','проверка')],['src/portable_os_admin/install/access/runtime/poa_accessd.py','src/portable_os_admin/install/access/runtime/poa_launch.py'])
    graph('06-storage','06 · Данные и места хранения — сейчас','Ключи и полезная нагрузка схемы показаны по назначению; реальных секретов и адресов стенда здесь нет.',[
        ('tk',0,0,'Ткач'),('cont',0,2,'Файл контейнера\nAES-GCM + scrypt','code','db'),('cc',0,4,'Конфигурации,\nзаписи, секрет админа'),
        ('nit',2,0,'Нить'),('state',2,2,'LUKS-хранилище\nсостояние устройства','code','db'),('logs',2,4,'LUKS-журналы\nлокальные события','code','db'),
        ('coc',4,0,'Кокон'),('db',4,2,'Серверная БД\nучёт / TOTP / группы','code','db'),('keys',6,2,'Файлы ключей\nsystemd credentials'),('releases',4,4,'Каталог выпусков\nобразы + подписи')],
        [('tk','cont','открыть'),('cont','cc','содержит'),('nit','state','постоянное'),('state','logs','служебный ключ'),('coc','db','учёт'),('keys','coc','при старте'),('coc','releases','раздача')],['src/portable_os_admin/container/models.py','packaging/revocation-server/poa_access/store.py','src/portable_os_admin/install/disk.py'])
    graph('07-trust','07 · Ключи и цепочки доверия — сейчас','Основной ключ на токене не интегрирован. Рабочая подпись текущего контейнера — Ed25519; целевой ГОСТ показан отдельно.',[
        ('root',0,0,'Резервный корень\nГОСТ · файл'),('cert',2,0,'Сертификат\nадминистратора'),('worker',4,0,'Рабочий ключ\nEd25519'),
        ('release',4,2,'Подпись выпуска\nи карточки сервера'),('anchors',0,2,'Открытые якоря\nдоверия','partial'),('verify',2,2,'Проверка цепочки\nв poa_trust'),
        ('device',0,4,'Ключ Нити\nподпись запросов'),('server',2,4,'Кокон проверяет\nключ Нити'),('ca',4,4,'Ключ CA\nVPN-сертификаты'),('serverkey',6,4,'Ключ Кокона\nподпись допуска'),('token',6,0,'Корень на токене\nи ГОСТ админа','target')],
        [('root','cert','заверяет'),('cert','worker','открытый ключ'),('worker','release','подписывает'),('anchors','verify','доверие'),('cert','verify','цепочка'),('release','verify','проверить'),('device','server','аутентификация'),('server','ca','выдать допуск'),('serverkey','verify','подлинность ответа'),('token','cert','целевая схема','target')],['src/portable_os_admin/admin/org_root.py','src/portable_os_admin/install/access/runtime/poa_trust.py','packaging/revocation-server/poa_access/ca.py'])
    graph('08-media-boot','08 · Разделы носителя и загрузка','Образ ОС не лежит внутри LUKS. dm-verity проверяет блоки образа; текущая схема не гарантирует доверие к ядру до его запуска.',[
        ('esp',0,0,'1 · POA_ESP\nUEFI / GRUB'),('boot',1,0,'2 · POA_BOOT\nядра / initrd A/B'),('state',2,0,'3 · POA_SYSTEM\nLUKS · состояние'),('logs',3,0,'4 · POA_LOGS\nLUKS · журналы'),('images',4,0,'5 · POA_IMAGES\nsquashfs / verity'),
        ('uefi',0,2,'UEFI → GRUB'),('kernel',2,2,'Ядро + initramfs\nвыбор слота'),('unlock',4,2,'Фраза + машина\nоткрыть LUKS'),
        ('verify',4,4,'dm-verity\nтолько чтение'),('ram',2,4,'RAM-слой\nвременные изменения'),('desktop',0,4,'Киоск → окно\nподключения')],
        [('esp','uefi','загрузка'),('boot','kernel','файлы'),('state','unlock','ключи'),('uefi','kernel','старт'),('kernel','unlock','разблокировка'),('images','verify','образ'),('unlock','verify','продолжить'),('verify','ram','корень ОС'),('ram','desktop','сеанс')],['src/portable_os_admin/install/disk.py','src/portable_os_admin/install/overlay.py','docs/LIVE-2.0.md'])
    sequence('09-issue','09 · Сборка выпуска и выдача «Нити»','Сборка образа и запись конкретного устройства — разные операции. Перезапись означает новую идентичность устройства.',
        ['Администратор','Ткач','Linux / накопитель','Контейнер','Кокон'],[
        (0,1,'Выбрать конфигурацию и исходный образ'),(1,2,'Собрать систему и подписанный выпуск'),(0,1,'Выбрать устройство, владельца и фразу'),(1,2,'Проверить накопитель и подтверждение перезаписи'),(1,2,'Разметить, записать ОС, LUKS и восстановление'),(1,3,'Сохранить ID, ключи и записи восстановления'),(1,4,'Опубликовать запись и выпуск при подключённом сервере'),(4,1,'Подтвердить учёт / назначения')],
        ['src/portable_os_admin/admin/media_release.py','src/portable_os_admin/install/installer.py','src/portable_os_admin/admin/revocation_publish.py'])
    sequence('10-access','10 · Активация и вход на рабочее место','Схема успешного входа после активации. Отказы: нет TOTP, блокировка, старая версия, запрет ВМ, признак клона.',
        ['Пользователь','Нить','Кокон','VPN-шлюз','Рабочее место'],[
        (0,1,'При первом запуске: код активации и настройка TOTP'),(1,2,'Запросить одноразовый nonce'),(0,1,'Ввести актуальный TOTP'),(1,2,'Подписанный /v3/session: TOTP, звено, статус'),(2,1,'Предложить новое звено / сообщить выпуск'),(1,2,'Сохранить звено и подтвердить /v3/confirm'),(2,1,'Подписанный допуск: сертификат VPN и цели'),(1,3,'Установить VPN и ограничить маршруты'),(1,4,'Запустить RDP / VNC / SSH'),(0,4,'Аутентификация на рабочем месте отдельно')],
        ['packaging/revocation-server/poa_access/v3.py','src/portable_os_admin/install/access/runtime/poa_accessd.py'])
    sequence('11-recovery','11 · Удалённое восстановление доступа','Текущий код использует общий секрет контейнера. Индивидуальные секреты Нитей и перенос полномочий — целевая доработка.',
        ['Пользователь','Нить','Администратор','Ткач / контейнер'],[
        (0,1,'Открыть восстановление пароля или привязок'),(1,0,'Показать запрос: устройство / слот / вид'),(0,2,'Передать запрос по согласованному каналу'),(2,3,'Проверить владельца и доступность записи'),(3,2,'Выдать код, отметить запись использованной'),(2,0,'Передать одноразовый код'),(0,1,'Ввести код и новую фразу / подтвердить сброс'),(1,0,'Обновить слоты, удалить использованный ключ восстановления')],
        ['src/portable_os_admin/recovery/service.py','src/portable_os_admin/recovery/recovery_module.sh'])
    graph('12-update','12 · Обновление A/B и возврат после сбоя','A/B — две области образа ОС. Успех подтверждается после проверки новой системы; антиоткат ограничивает допустимые версии.',[
        ('offer',0,0,'Кокон / каталог\nновый выпуск'),('check',2,0,'Подпись, версия,\nсемейство, разметка'),('valid',4,0,'Подходит?','code','decision'),
        ('deny',6,0,'Отказ / прежняя ОС'),('write',4,2,'Неактивный слот\nзагрузка .part'),('hash',2,2,'Проверить хеши\nвсех файлов'),('try',0,2,'Назначить пробный\nслот · 3 попытки'),
        ('boot',0,4,'Перезагрузить\nпроверить dm-verity'),('ok',2,4,'Подтверждён?','code','decision'),('good',4,4,'Закрепить новый\nрабочий слот'),('rollback',6,4,'Вернуть прежний\nдопустимый слот')],
        [('offer','check','метаданные'),('check','valid','проверка'),('valid','deny','нет'),('valid','write','да'),('write','hash','записано'),('hash','try','совпало'),('try','boot','следующий старт'),('boot','ok','результат'),('ok','good','да'),('ok','rollback','нет / попытки исчерпаны')],['src/portable_os_admin/admin/media_update.py','src/portable_os_admin/install/access/runtime/poa_update.py'])
    graph('13-revocation','13 · Блокировка, отзыв и уничтожение','Блокировка и отзыв — разные действия. Серверное решение не означает, что отключённая Нить уже выполнила уничтожение.',[
        ('decision',0,0,'Администратор\nрешение'),('tk',2,0,'Ткач\nподписать команду'),('server',4,0,'Кокон\nзаписать состояние'),('block',6,0,'Блокировка\nотказать в допуске'),
        ('offline',4,2,'Нить без связи\nкоманда не доставлена','external'),('online',2,2,'Нить: запрос\nпроверки отзыва'),('signed',0,2,'Проверить подпись\nответа revoked'),
        ('wipe',0,4,'Уничтожить ключевые\nобласти и данные'),('power',2,4,'Выключить\nнужен перевыпуск'),('local',4,4,'Физически подключена\nлокальное стирание')],
        [('decision','tk','отзыв'),('tk','server','счётчик команды'),('server','block','отдельное действие'),('server','offline','нет доставки'),('online','server','опрос'),('server','signed','подписанный ответ'),('signed','wipe','подлинный отзыв'),('wipe','power','завершение'),('local','wipe','с подтверждением')],['src/portable_os_admin/admin/media_revoke.py','src/portable_os_admin/install/revocation/script.py','packaging/revocation-server/poa_access/v3.py'])
    sequence('14-exchange','14 · Обмен конфигурациями — сейчас','Посылка переносит конфигурации. Передача записей Нитей, их секретов и серверного владения в этот протокол не входит.',
        ['Получатель','Ткач отправителя','Файл посылки','Ткач получателя'],[
        (0,1,'Передать публичный код администратора'),(1,2,'Зашифровать конфигурации для получателя: X25519 + AES-GCM'),(1,2,'Подписать посылку ключом Ed25519'),(2,3,'Передать файл любым согласованным каналом'),(3,0,'Проверить адресата, подпись и код отправителя'),(0,3,'Подтвердить импорт выбранных конфигураций')],
        ['src/portable_os_admin/config/exchange.py','src/portable_os_admin/gui/screens/configs_share.py'])
    graph('15-lifecycle','15 · Жизненный цикл устройства','Логическая модель сценариев. Это не перечень значений одного поля БД: локальная запись и серверное состояние различаются.',[
        ('blank',0,0,'Пустой накопитель','external'),('issued',2,0,'Выпущена\nзапись у админа'),('active',4,0,'Активирована\nдопуск настроен'),('session',6,0,'Рабочий сеанс'),
        ('block',4,2,'Заблокирована\nдопуск закрыт'),('recover',2,2,'Восстановление\nпароля / машин'),('revoked',4,4,'Отозвана\nсерверное состояние'),('erased',2,4,'Уничтожена /\nперезаписана'),('new',0,4,'Новая Нить\nновая идентичность')],
        [('blank','issued','выпуск'),('issued','active','активация'),('active','session','вход'),('session','active','отключение'),('active','block','ошибки / клон'),('block','active','разблокировка'),('issued','recover','утрата доступа'),('recover','issued','новая фраза'),('active','revoked','отзыв'),('revoked','erased','доставлен / физически'),('erased','new','новый выпуск')],['src/portable_os_admin/container/models.py','packaging/revocation-server/poa_access/v3.py'])
    graph('16-data-model','16 · Серверные данные и связи','Логические связи по идентификаторам; схема не утверждает наличие внешних ключей SQL во всех таблицах.',[
        ('media',2,2,'media\nPK media_id\nowner / group_id','code','db'),('group',0,0,'groups\nPK id\nparent / distribution','code','db'),('dist',2,0,'distributions\nPK id','code','db'),('rel',4,0,'releases\ndistribution + serial','code','db'),
        ('access',0,2,'access\nmedia_id\nTOTP / link / fails','code','db'),('targets',4,2,'targets\nmedia_id / target_id','code','db'),('cert',6,2,'certs\nserial / media_id','code','db'),
        ('nonce',0,4,'activations / nonces3\nодноразовые значения','code','db'),('events',2,4,'device_events\nmedia_id + seq','code','db'),('history',4,4,'link_history\nmedia_id + h','code','db'),('log',6,4,'server_log\nсобытия сервера','code','db')],
        [('group','media','1 : N'),('dist','group','1 : N'),('dist','rel','1 : N'),('media','access','1 : 1'),('media','targets','1 : N'),('media','cert','1 : N'),('media','nonce','1 : N'),('media','events','1 : N'),('media','history','1 : N'),('media','log','логическая ссылка')],['packaging/revocation-server/poa_access/store.py'])
    graph('17-network-stages','17 · Сетевой доступ по стадиям','Правила nftables дополняют ограничения киоска. DNS / DHCP / синхронизация времени относятся к базовой инфраструктуре.',[
        ('boot',0,0,'После загрузки\nвходящие закрыты'),('infra',2,0,'Инфраструктура\nDNS / DHCP / время'),('server',4,0,'Разрешён Кокон\nTLS и проба сети'),
        ('portal',4,2,'Портал Wi-Fi\nотдельный пользователь'),('grant',2,2,'Допуск выдан\nразрешить VPN-шлюз'),('vpn',0,2,'VPN поднят\nмаршрут подтверждён'),
        ('targets',0,4,'Назначенные цели\nтолько через туннель'),('end',2,4,'Отключение VPN\nубрать допуск целей'),('base',4,4,'Возврат к базовым\nсетевым разрешениям')],
        [('boot','infra','база'),('infra','server','связь'),('server','portal','нужна веб-авторизация'),('portal','server','сеть открыта'),('server','grant','проверки пройдены'),('grant','vpn','туннель'),('vpn','targets','доступ'),('targets','end','выход'),('end','base','сброс')],['src/portable_os_admin/install/access/runtime/poa_firewall.py','src/portable_os_admin/install/access/runtime/poa_accessd.py'])
    graph('18-ha','18 · Отказоустойчивость «Кокона»','Кластерный вариант из сценариев развёртывания. Автопереключение адреса клиента не равно автоматическому поиску новых серверов.',[
        ('nit',0,0,'Нить / Ткач'),('endpoints',2,0,'Карточка сервера\nдоверенные адреса','partial'),('app1',0,2,'Узел 1\nHTTP + Patroni'),('app2',2,2,'Узел 2\nHTTP + Patroni'),('app3',4,2,'Узел 3\nHTTP + Patroni'),
        ('etcd',6,2,'etcd · кворум\nвыбор лидера'),('pg1',0,4,'PostgreSQL\nтекущий лидер','code','db'),('pg2',2,4,'PostgreSQL\nреплика','code','db'),('pg3',4,4,'PostgreSQL\nреплика','code','db'),('files',6,4,'Доставка файлов\nвыпусков: проверить\nперед эксплуатацией','partial')],
        [('nit','endpoints','TLS'),('endpoints','app1','адрес 1'),('endpoints','app2','адрес 2'),('endpoints','app3','адрес 3'),('app1','pg1','libpq read-write'),('app2','pg1','libpq read-write'),('app3','pg1','libpq read-write'),('pg1','pg2','репликация TLS'),('pg1','pg3','репликация TLS'),('app3','etcd','координация')],['packaging/revocation-server/cluster.sh','src/portable_os_admin/install/access/runtime/poa_accessd.py'])
    graph('19-target','19 · Целевая модель администрирования','ЦЕЛЕВАЯ СХЕМА. «Основа» — предложенное имя. Два токена — новое требование; копии полномочий не означают одинаковые неизвлекаемые ключи.',[
        ('root',0,0,'Основа\nосновной носитель','target'),('reserve',2,0,'Резерв\nтокен или файл','target'),('cert',4,0,'Корневой допуск\nадминистраторов','target'),
        ('token',0,2,'Токен админа\nнеизвлекаемый ключ','target'),('tk',2,2,'Ткач\nрежим администратора','target'),('coc',4,2,'Кокон\nреестр / конфигурации','target'),
        ('records',0,4,'Записи своих Нитей\nна токене админа','target'),('escrow',2,4,'Депонированные\nзаписи для передачи','target'),('sealed',4,4,'Секреты сервера\nаварийный слот Основы','target')],
        [('root','reserve','резерв полномочий','target'),('root','cert','подписывает','target'),('cert','token','сертификат','target'),('token','tk','подписи','target'),('tk','coc','управление','target'),('token','records','хранение','target'),('records','escrow','зашифрованная копия','target'),('coc','escrow','хранение','target'),('token','sealed','слот администратора','target')],['docs/SOLUTION.md','src/portable_os_admin/admin/org_root.py'])
    graph('20-gaps','20 · Переход от текущего к целевому','Схема разрывов реализации. Пунктир означает задачу разработки, а не уже работающий канал.',[
        ('a',0,0,'Файловый контейнер'),('at',3,0,'Токен администратора','target'),('b',0,1,'Конфигурации\nв контейнере'),('bt',3,1,'Зашифрованные\nконфигурации на сервере','target'),
        ('c',0,2,'Посылки\nконфигураций'),('ct',3,2,'Передача Нитей\nсекреты + владение','target'),('d',0,3,'Общий секрет админа\nдля восстановления'),('dt',3,3,'Свой секрет\nкаждой Нити','target'),
        ('e',0,4,'Код знакомства\nс сервером'),('et',3,4,'Обязательная цепочка\nдопуска от корня','target'),('f',0,5,'Файлы ключей\nсервера'),('ft',3,5,'Запечатанный сервер\nи аварийные слоты','target')],
        [('a','at','заменить','target'),('b','bt','перенести','target'),('c','ct','расширить','target'),('d','dt','разделить','target'),('e','et','связать','target'),('f','ft','реализовать','target')],['src/portable_os_admin/container/manager.py','src/portable_os_admin/recovery/service.py','docs/SOLUTION.md'])
    sequence('21-end-session','21 · Завершение работы и границы ответственности','Закрыть окно RDP, отключить VPN и выйти из учётной записи рабочего места — разные действия. Единый сценарий требует доработки.',
        ['Пользователь','Клиент рабочего места','Служба Нити','VPN / firewall'],[
        (0,1,'Отключиться или завершить удалённый сеанс'),(1,0,'Вернуться в окно подключения'),(0,2,'Запросить отключение VPN'),(2,3,'Остановить службу poa-vpn и ограничить сеть'),(0,2,'Завершить работу Нити / выключить компьютер'),(2,0,'Единая политика очистки и отказов ещё не закреплена','target')],
        ['src/portable_os_admin/install/access/runtime/poa_connect.py','src/portable_os_admin/install/access/runtime/poa_accessd.py'])
    graph('22-security','22 · Угрозы, барьеры и пределы защиты','Архитектурная карта, не заключение аудита. Скомпрометированное железо хозяина остаётся существенной границей доверия.',[
        ('loss',0,0,'Кража накопителя','external'),('luks',2,0,'LUKS + фраза\nмашинные слоты'),('clone',0,1,'Копия flash-Нити','external'),('link',2,1,'Смена звена\nобнаружение повтора'),
        ('mitm',0,2,'Подмена сервера','external'),('tls',2,2,'TLS pin + подпись\nкарточки / ответа'),('image',0,3,'Порча образа ОС','external'),('verity',2,3,'Подпись выпуска\nи dm-verity'),
        ('host',4,0,'Враждебный\nкомпьютер хозяина','external'),('limit',6,0,'Нет полной защиты\nот вредоносного железа','external'),('boot',4,2,'Подмена ядра\nдо его запуска','external'),('stage',6,2,'Проверяющая ступень\nтокена-носителя','target'),('phone',4,4,'Кража токена + PIN','external'),('totp',6,4,'TOTP отдельно\nна телефоне')],
        [('loss','luks','барьер'),('clone','link','детектирование'),('mitm','tls','подлинность'),('image','verity','целостность'),('host','limit','ограничение'),('boot','stage','целевая защита','target'),('phone','totp','дополнительный фактор')],['docs/THREAT-MODEL.md','src/portable_os_admin/install/machine_binding.py','src/portable_os_admin/install/access/runtime/poa_trust.py'])


def validate(pages):
    nodes = edges = 0
    for item in pages:
        root = ET.fromstring(item['page'].xml(0))
        cells = root.findall('.//mxCell')
        ids = [c.attrib['id'] for c in cells]
        assert len(ids) == len(set(ids)), item['slug']
        for c in cells:
            if c.get('edge') == '1':
                assert c.find('mxGeometry') is not None
                for attr in ('source', 'target'):
                    assert not c.get(attr) or c.get(attr) in ids
                edges += 1
            elif c.get('vertex') == '1':
                g = c.find('mxGeometry')
                assert float(g.get('y')) + float(g.get('height')) <= 958, item['slug']
                nodes += 1
    return nodes, edges


def main():
    build()
    nodes, edges = validate(PAGES)
    (OUT / 'diagrams').mkdir(parents=True, exist_ok=True)
    (OUT / 'svg').mkdir(exist_ok=True)
    write(str(OUT / 'shleif-architecture.drawio'), [i['page'] for i in PAGES])
    for item in PAGES:
        write(str(OUT / 'diagrams' / (item['slug'] + '.drawio')), [item['page']])
    manifest = [{k: v for k, v in i.items() if k != 'page'} for i in PAGES]
    (OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    intro = '# Архитектура «Шлейфа»\n\nСрез исходного кода `' + REVISION + '` ветки `live-2.0`, ' + DATE + '.\n\n'
    intro += 'Открой [атлас draw.io](shleif-architecture.drawio) либо [галерею](index.html). Каждая страница также доступна отдельно в `diagrams/` и как SVG в `svg/`.\n\n'
    intro += 'Синий — механизм присутствует в коде; жёлтый — частичная интеграция; фиолетовый пунктир — целевой механизм; серый — внешний участник или пояснение. Наличие кода не означает подтверждение на живом стенде. «Основа» — предложенное, ещё не утверждённое имя.\n\n'
    intro += '| № | Схема | Что объясняет |\n|---|---|---|\n'
    for i, item in enumerate(PAGES, 1):
        intro += f'| {i} | [{item["title"]}](diagrams/{item["slug"]}.drawio) | {item["note"]} |\n'
    intro += '\n## Как читать\n\nОбзор: 01–02 → компоненты: 03–05 → данные и доверие: 06–08 → операции: 09–15 → данные, сеть и отказы: 16–18 → цель и разрывы: 19–20 → завершение и угрозы: 21–22.\n\n'
    intro += 'Источники каждой страницы указаны в её подвале и в `manifest.json`. Схема БД логическая, а не точный SQL DDL. Состояния устройства — модель сценариев, а не единое поле БД. Старый `docs/SOLUTION.drawio` сохранён как историческое описание целевого решения.\n\n'
    intro += '## Воспроизведение\n\n`python3 tools/diagram/architecture_atlas.py` создаёт XML и проверяет ссылки, уникальность ID и границы страницы. Экспорт всех SVG: `python3 tools/diagram/export_architecture.py`. Для PNG добавь `--png-dir /tmp/shleif-preview`. Для отдельного листа: `--only 08`. Нужен установленный draw.io Desktop. Для проверки расположения дополнительно открой отрисованные страницы. Это проверка документации, не работоспособности всей системы.\n'
    (OUT / 'README.md').write_text(intro)
    cards = ''.join(f'<section id="{i["slug"]}"><h2>{html.escape(i["title"])}</h2><p>{html.escape(i["note"])}</p><a href="svg/{i["slug"]}.drawio.svg"><img loading="lazy" src="svg/{i["slug"]}.drawio.svg" alt="{html.escape(i["title"])}"></a><p><a href="diagrams/{i["slug"]}.drawio">Редактировать в draw.io</a></p></section>' for i in PAGES)
    (OUT / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Архитектура Шлейфа</title><style>body{font:16px system-ui;background:#f3f6fa;color:#172b4d;max-width:1320px;margin:32px auto;padding:0 20px}section{background:white;padding:24px;margin:24px 0;border:1px solid #dce3ec;border-radius:12px}img{width:100%;height:auto}a{color:#175da6}p{line-height:1.5}@media print{section{break-after:page}}</style><h1>Шлейф · Атлас архитектуры</h1><p>22 редактируемые схемы · 11.11.2026 · код 43faf8a. Синий — код; жёлтый — частично; фиолетовый пунктир — цель. «Основа» — предложенное название.</p><a href="shleif-architecture.drawio">Открыть многостраничный исходник</a>' + cards + '</html>')
    print(f'Схем: {len(PAGES)}; блоков: {nodes}; соединений: {edges}. XML и источники проверены.')


if __name__ == '__main__':
    main()
