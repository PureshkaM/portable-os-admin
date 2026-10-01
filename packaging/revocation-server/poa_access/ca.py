"""Удостоверяющий центр для OpenVPN: сертификаты клиентов на сутки и CRL.

Почему свой CA, а не учётные записи на шлюзе. Шлюз компании доверяет CA и
забирает CRL раз в минуту -- больше он с нашим сервером не говорит (модель
ViPNet: центр выпускает, координатор проверяет). Сертификат живёт сутки, ключ к
нему создан на носителе и наружу не выходил, поэтому долгоживущего секрета для
VPN на носителе нет вовсе.
"""
from __future__ import annotations

import datetime
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

#: Срок сертификата клиента -- ровно рабочий день с запасом: сутки.
CLIENT_LIFETIME = datetime.timedelta(hours=24)
#: Запас на расхождение часов шлюза и сервера.
BACKDATE = datetime.timedelta(minutes=5)
#: CRL перевыпускается не реже, чем шлюз его забирает, с запасом.
CRL_LIFETIME = datetime.timedelta(hours=1)


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def new_ca(org: str) -> tuple[bytes, bytes]:
    """Новый CA: (ключ PEM, сертификат PEM). Делает сценарий установки."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"poa VPN CA {org}"[:64])])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now() - BACKDATE)
        .not_valid_after(now() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    )


def load_client_public_key(pem: str):
    """Открытый ключ клиента VPN; только P-256 -- то, что создаёт носитель."""
    key = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("ожидается ключ ECDSA P-256")
    return key


def issue_client(ca_key, ca_cert_pem: bytes, media: str, public_key) -> tuple[bytes, int, int]:
    """Сертификат клиента: (PEM, серийный номер, not_after в unix-секундах)."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    serial = int.from_bytes(secrets.token_bytes(16), "big") >> 1
    not_after = now() + CLIENT_LIFETIME
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, media)]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(now() - BACKDATE)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=True, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), serial, int(not_after.timestamp())


def crl(ca_key, ca_cert_pem: bytes, revoked: list[tuple[int, int]]) -> bytes:
    """CRL в PEM. `revoked` -- пары (серийный номер, время отзыва unix)."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now() - BACKDATE)
        .next_update(now() + CRL_LIFETIME)
    )
    for serial, when in revoked:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(datetime.datetime.fromtimestamp(when, datetime.timezone.utc))
            .build()
        )
    return builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def issue_server(ca_key, ca_cert_pem: bytes, name: str) -> tuple[bytes, bytes]:
    """Ключ и сертификат VPN-шлюза: (ключ PEM, сертификат PEM).

    Клиент сверяет имя (`verify-x509-name`) и назначение (`remote-cert-tls
    server`), поэтому имя -- в CN и в SAN, назначение -- serverAuth. Срок --
    два года: шлюз меняется вручную, а не каждый день.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now() - BACKDATE)
        .not_valid_after(now() + datetime.timedelta(days=730))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=True, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    )
