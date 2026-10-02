"""Э22: доверие TLS к репозиториям -- промежуточный УЦ только с подписью корня дистрибутива."""
from __future__ import annotations

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

from portable_os_admin.install import repo_trust


def _cert(name, key, issuer_name, issuer_key, ca, aia=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
               .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if aia:
        builder = builder.add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(
            AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(aia))]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _pki():
    root_key, sub_key, leaf_key, evil_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(4))
    root = _cert("Root", root_key, "Root", root_key, True)
    sub = _cert("Sub 2024", sub_key, "Root", root_key, True)
    forged = _cert("Sub 2024", evil_key, "Root", evil_key, True)  # то же имя, чужая подпись
    leaf = _cert("repo", leaf_key, "Sub 2024", sub_key, False, aia="http://ca.example/sub.crt")
    return root, sub, forged, leaf


def test_an_intermediate_is_accepted_only_by_signature_not_by_name():
    root, sub, forged, _leaf = _pki()
    assert repo_trust.issued_by_anchor(sub, [root])
    assert not repo_trust.issued_by_anchor(forged, [root])


def test_missing_intermediate_is_fetched_by_aia_and_checked(monkeypatch):
    root, sub, forged, leaf = _pki()
    monkeypatch.setattr(repo_trust, "_final_host", lambda url: "repo.example")
    monkeypatch.setattr(repo_trust, "_peer_leaf", lambda host: leaf)
    served = {"http://ca.example/sub.crt": sub.public_bytes(serialization.Encoding.DER)}
    got = repo_trust.missing_intermediates(["https://repo.example/x"], [root], fetch=served.__getitem__)
    assert got == [sub]
    # Подменённый по дороге промежуточный -- не принимается.
    served["http://ca.example/sub.crt"] = forged.public_bytes(serialization.Encoding.PEM)
    assert repo_trust.missing_intermediates(["https://repo.example/x"], [root], fetch=served.__getitem__) == []


def test_no_anchors_on_the_image_means_no_bundle(tmp_path):
    assert repo_trust.build_bundle(["https://repo.example/x"], str(tmp_path)) == ""
