"""Стенд: корень организации и сертификат контейнера (для подписанных выпусков)."""
import pathlib, sys
sys.path.insert(0, "src")
from portable_os_admin.container import ContainerManager
from portable_os_admin.admin import org_root
CONT = pathlib.Path(sys.argv[1]); ROOT = pathlib.Path("/root/e9-run/reserve.poaroot")
ROOT.parent.mkdir(exist_ok=True)
if not ROOT.exists():
    phrase, entry = org_root.create_reserve_root(ROOT)
    (ROOT.parent / "phrase").write_text(phrase)
phrase = (ROOT.parent / "phrase").read_text()
c = ContainerManager().open(CONT, "AdminContainer123")
org_root.set_anchors(c, [org_root.read_reserve_entry(ROOT)])
org_root.certify_container(org_root.open_reserve_root(ROOT, phrase), c, "Стенд")
ContainerManager().save(c)
print("якоря:", [org_root.fingerprint(a) for a in org_root.anchors(c)], "сертификат:", bool(c.data.working_key_cert_json))
