"""Э18: цепочка доверия корень → рабочий ключ → описатель."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime"
_spec = importlib.util.spec_from_file_location("poa_trust", RUNTIME / "poa_trust.py")
trust = importlib.util.module_from_spec(_spec)
sys.modules["poa_trust"] = trust
_spec.loader.exec_module(trust)


@pytest.fixture
def org():
    root = ed25519.Ed25519PrivateKey.generate()
    working = ed25519.Ed25519PrivateKey.generate()
    cert = trust.certify(root, trust.raw_public(working), time.time() + 3600, 1)
    return root, working, cert, trust.b64(trust.raw_public(root))


def _descriptor(working, cert, serial, endpoints=("https://a.example",)):
    return trust.sign_document(working, cert, "descriptor",
                               {"format": 1, "serial": serial, "endpoints": list(endpoints)})


def test_a_descriptor_signed_by_a_certified_key_is_accepted(org):
    root, working, cert, root_pub = org
    payload = trust.verify_document(root_pub, _descriptor(working, cert, 3), "descriptor")
    assert payload["serial"] == 3


def test_a_key_certified_by_another_root_is_refused(org):
    _root, working, cert, _ = org
    stranger = trust.b64(trust.raw_public(ed25519.Ed25519PrivateKey.generate()))
    with pytest.raises(trust.TrustError):
        trust.verify_document(stranger, _descriptor(working, cert, 1), "descriptor")


def test_tampered_payload_is_refused(org):
    root, working, cert, root_pub = org
    doc = _descriptor(working, cert, 1)
    doc["descriptor"]["endpoints"] = ["https://evil.example"]
    with pytest.raises(trust.TrustError, match="подпись"):
        trust.verify_document(root_pub, doc, "descriptor")


def test_expired_and_revoked_working_keys_are_refused(org):
    root, working, cert, root_pub = org
    with pytest.raises(trust.TrustError, match="срок"):
        trust.verify_document(root_pub, _descriptor(working, cert, 1), "descriptor",
                              now=time.time() + 7200)
    revoked = trust.revocation_list(root, [trust.raw_public(working)], 1)
    with pytest.raises(trust.TrustError, match="отозван"):
        trust.verify_document(root_pub, _descriptor(working, cert, 1), "descriptor", revoked)


def test_an_old_genuine_descriptor_does_not_roll_back(org):
    root, working, cert, root_pub = org
    current = {"serial": 5}
    assert trust.newer_descriptor(root_pub, _descriptor(working, cert, 4), current) is None
    assert trust.newer_descriptor(root_pub, _descriptor(working, cert, 6), current)["serial"] == 6


def test_a_working_key_cannot_certify_itself(org):
    root, working, cert, root_pub = org
    forged = trust.certify(working, trust.raw_public(working), time.time() + 3600, 2)
    with pytest.raises(trust.TrustError):
        trust.verify_document(root_pub, _descriptor(working, forged, 1), "descriptor")
