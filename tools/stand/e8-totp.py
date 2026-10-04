"""Стенд: код TOTP носителя по семени с сервера (вместо телефона)."""
import pathlib, sqlite3, sys, time
sys.path.insert(0, "/root/poa-live2/packaging/revocation-server")
from poa_access import secrets_store, totp
media = sys.argv[1]
s = secrets_store.load(pathlib.Path("/root/e6/keys"), pathlib.Path("/root/e6/keys/revocation-key"))
blob = sqlite3.connect("/root/e6/state/media.sqlite").execute("SELECT totp FROM access WHERE media_id=?", (media,)).fetchone()[0]
secret = s.unseal("totp", media, blob).decode()
print(totp.code_at(secret, int(time.time()) // totp.PERIOD))
