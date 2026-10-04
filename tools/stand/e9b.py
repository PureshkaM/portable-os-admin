"""Сквозная проверка доступа на стенде (диагностика перед GUI-прогоном)."""
import os, pathlib, secrets, subprocess, sys, time
from datetime import datetime
sys.path.insert(0, "src")
os.environ["POA_ALLOW_NON_REMOVABLE"] = "1"
URL = "https://10.0.2.2:8443"
FAMILY = sys.argv[2] if len(sys.argv) > 2 else "debian"
CONT = pathlib.Path(f"/root/e8-run/{FAMILY}.container"); PW = "AdminContainer123"
OUT = pathlib.Path(sys.argv[3] if len(sys.argv) > 3 else "/root/e9-dist2"); IMG = f"/root/e8-{FAMILY}.img"
IMAGE = {"debian": "/root/images/debian-13.6.0-amd64-DVD-1.iso", "redos": "/root/images/redos-8с.iso"}[FAMILY]
def log(m): print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)
from portable_os_admin.container import ContainerManager
from portable_os_admin.session import AdminSession
from portable_os_admin.admin import revocation_client, revocation_publish, access_actions

def session():
    return AdminSession(ContainerManager().open(CONT, PW))

cmd = sys.argv[1]
if cmd == "setup":
    CONT.parent.mkdir(parents=True, exist_ok=True)
    if CONT.exists(): CONT.unlink()
    container = ContainerManager().create(CONT, PW)
    code = secrets.token_hex(16)
    pathlib.Path("/root/e6/state/enrollment-code").write_text(code + "\n")
    pem = revocation_client.fetch_public_key(URL)
    log(revocation_client.enroll(URL, container.admin_authority_secret, code))
    container.data.revocation_server_url = URL; container.data.revocation_public_key_pem = pem
    ContainerManager().save(container)
    s = session(); log(f"подключён: {access_actions.is_connected(s.container)}")
    access_actions.client(s.container).set_vpn_remotes([{"host": "10.0.2.2", "port": 1194, "proto": "udp"}], "gw.stand")
    log("шлюз задан")
elif cmd == "build":
    from portable_os_admin.config import ConfigManager
    from portable_os_admin.config.schema import InstallConfig
    from portable_os_admin.install import OSInstaller
    s = session()
    config = InstallConfig.create_baseline(f"e8-{FAMILY}", FAMILY)
    config.arch = "amd64" if FAMILY == "debian" else "x86_64"
    config.account.username = "kiosk"; config.account.full_name = "Kiosk"
    config.revocation.enabled = True; config.revocation.url = URL
    ConfigManager().validate(config)
    log(f"конфигурация: {config.validation_status} {config.validation_errors}")
    if config.validation_status != "valid": sys.exit(1)
    OUT.mkdir(parents=True, exist_ok=True)
    last = [""]
    def on_step(i, n, t):
        if t != last[0]: log(f"этап {i}/{n}: {t}"); last[0] = t
    res = OSInstaller(s).build_distribution(config, str(OUT), image_path=IMAGE, dry_run=False, on_step=on_step)
    log(f"собрано: {res.metadata.rootfs_bytes/1e6:.0f} МБ")
elif cmd == "deploy":
    from portable_os_admin.install import OSInstaller
    from portable_os_admin.install.devices import DeviceLister
    subprocess.run(["sh", "-c", f"losetup -j {IMG} | cut -d: -f1 | xargs -r -n1 losetup -d; rm -f {IMG}; truncate -s 14G {IMG}"], check=True)
    loop = subprocess.run(["losetup", "-f", "--show", "-P", IMG], check=True, capture_output=True, text=True).stdout.strip()
    s = session(); last = [""]
    def on_step(i, n, t):
        if t != last[0]: log(f"шаг {i}/{n}: {t}"); last[0] = t
    res = OSInstaller(s).deploy_distribution(DeviceLister().describe(loop), str(OUT), b"KioskTest12345", dry_run=False,
                                             confirm_device=loop, on_step=on_step, media_name="e8", media_owner="Стенд")
    subprocess.run(["losetup", "-d", loop])
    media = res.media_record.media_id
    log(f"носитель {media}")
    log(revocation_publish.publish_record(s.container, media))
    access_actions.set_targets(s.container, media, [{"id": "ws1", "name": "Рабочее место", "type": "rdp", "host": "10.99.0.1",
        "port": 3389, "user": "rdpuser", "domain": "", "pin": "sha256:37b00521e24d56161219debeca8e4c7b481af979951090c67834319ceafa4204", "smartcard": False, "security": "tls"}])
    code = access_actions.issue_activation(s.container, media)
    ContainerManager().save(s.container)
    pathlib.Path("/root/e8-code").write_text(code + "\n")
    log(f"код активации: {code}")
