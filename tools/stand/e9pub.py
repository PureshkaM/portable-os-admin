"""Стенд Э9: опубликовать собранный дистрибутив как выпуск и назначить корневой группе."""
import json, pathlib, sys
sys.path.insert(0, "src")
from portable_os_admin.container import ContainerManager
from portable_os_admin.admin import access_actions
DIST = pathlib.Path(sys.argv[1]); NAME = sys.argv[2]
c = ContainerManager().open(pathlib.Path("/root/e8-run/debian.container"), "AdminContainer123")
release = json.loads((DIST / "release.json").read_text())["release"]
cl = access_actions.client(c)
cl.distribution(NAME, release["family"], release["arch"], release["layout"])
cl.assign("root", NAME)
files = [str(DIST / n) for n in ("release.json", "rootfs.squashfs", "rootfs.verity", "vmlinuz", "initrd.img")]
cl.publish_release(NAME, release["serial"], files)
print("опубликован выпуск", NAME, release["serial"])
