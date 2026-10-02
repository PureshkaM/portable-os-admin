"""Э18: цепочка доверия -- два корня ГОСТ, сертификаты администраторов, замена корня."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime"
sys.path.insert(0, str(RUNTIME))
_spec = importlib.util.spec_from_file_location("poa_trust", RUNTIME / "poa_trust.py")
trust = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trust)
gost = sys.modules["poa_gost"]


def _root(role=trust.ROLE_RESERVE):
    return trust.GostSigner(gost.new_private_key(gost.TC26_256_A), role)


@pytest.fixture(scope="module")
def org():
    main, reserve = _root(trust.ROLE_MAIN), _root(trust.ROLE_RESERVE)
    working = ed25519.Ed25519PrivateKey.generate()
    cert = trust.certify(main, trust.raw_public(working), time.time() + 3600, 1, "Иванов")
    return main, reserve, working, cert, [main.entry, reserve.entry]


def _descriptor(working, cert, serial):
    return trust.sign_document(working, cert, "descriptor",
                               {"serial": serial, "endpoints": ["https://a.example"]})


def test_a_descriptor_signed_by_a_certified_key_is_accepted(org):
    _main, _reserve, working, cert, anchors = org
    assert trust.verify_document(anchors, _descriptor(working, cert, 3), "descriptor")["serial"] == 3


def test_either_root_can_certify(org):
    _main, reserve, working, _cert, anchors = org
    cert = trust.certify(reserve, trust.raw_public(working), time.time() + 3600, 2)
    assert trust.verify_document(anchors, _descriptor(working, cert, 1), "descriptor")


def test_a_key_certified_by_a_foreign_root_is_refused(org):
    _main, _reserve, working, _cert, anchors = org
    cert = trust.certify(_root(), trust.raw_public(working), time.time() + 3600, 1)
    with pytest.raises(trust.TrustError, match="не корнем"):
        trust.verify_document(anchors, _descriptor(working, cert, 1), "descriptor")


def test_a_foreign_root_cannot_pose_as_ours_by_issuer(org):
    """Чужой корень подписал, но в issuer вписан наш: подпись не сойдётся."""
    main, _reserve, working, _cert, anchors = org
    cert = trust.certify(_root(), trust.raw_public(working), time.time() + 3600, 1)
    cert["issuer"] = trust.key_id(main.entry)
    with pytest.raises(trust.TrustError, match="подпись"):
        trust.verify_document(anchors, _descriptor(working, cert, 1), "descriptor")


def test_tampered_payload_is_refused(org):
    _main, _reserve, working, cert, anchors = org
    doc = _descriptor(working, cert, 1)
    doc["descriptor"]["endpoints"] = ["https://evil.example"]
    with pytest.raises(trust.TrustError, match="подпись"):
        trust.verify_document(anchors, doc, "descriptor")


def test_expired_and_revoked_working_keys_are_refused(org):
    main, reserve, working, cert, anchors = org
    with pytest.raises(trust.TrustError, match="срок"):
        trust.verify_document(anchors, _descriptor(working, cert, 1), "descriptor",
                              now=time.time() + 7200)
    revoked = trust.revocation_list(reserve, [trust.raw_public(working)], 1)
    with pytest.raises(trust.TrustError, match="отозван"):
        trust.verify_document(anchors, _descriptor(working, cert, 1), "descriptor", revoked)


def test_an_old_genuine_descriptor_does_not_roll_back(org):
    _main, _reserve, working, cert, anchors = org
    current = {"serial": 5}
    assert trust.newer_descriptor(anchors, _descriptor(working, cert, 4), current) is None
    assert trust.newer_descriptor(anchors, _descriptor(working, cert, 6), current)["serial"] == 6


def test_a_working_key_cannot_certify_itself(org):
    _main, _reserve, working, _cert, anchors = org

    class Ed:
        entry = {"alg": trust.ALG_ED25519, "key": trust.b64(trust.raw_public(working))}

        def sign(self, data):
            return working.sign(data)

    forged = trust.certify(Ed(), trust.raw_public(working), time.time() + 3600, 2)
    with pytest.raises(trust.TrustError):
        trust.verify_document(anchors, _descriptor(working, forged, 1), "descriptor")


def test_the_reserve_root_replaces_the_main_one(org):
    main, reserve, _working, _cert, anchors = org
    new = _root(trust.ROLE_MAIN)
    after = trust.apply_root_replacement(anchors, trust.root_replacement(reserve, main.entry, new.entry, 1))
    assert trust.key_id(main.entry) not in {trust.key_id(a) for a in after}
    assert reserve.entry in after and {**new.entry, "role": "main"} in after


def test_a_stolen_main_root_cannot_replace_the_reserve(org):
    """Симметричное правило дало бы вору основного корня заменить резервный на
    свой и закрепиться навсегда. Асимметрия это запрещает -- и при выпуске,
    и при приёме носителем (подпись подделана вручную в обход выпуска)."""
    main, reserve, _working, _cert, anchors = org
    thief = _root(trust.ROLE_RESERVE)
    with pytest.raises(trust.TrustError):
        trust.root_replacement(main, reserve.entry, thief.entry, 1)
    body = {"kind": "poa-root-replace", "old": reserve.entry, "new": thief.entry, "serial": 1}
    forged = {"replace": body, "issuer": trust.key_id(main.entry),
              "signature": trust.b64(main.sign(trust.canonical(body)))}
    with pytest.raises(trust.TrustError, match="только резервный"):
        trust.apply_root_replacement(anchors, forged)


def test_a_foreign_root_cannot_replace_anything(org):
    main, _reserve, _working, _cert, anchors = org
    stranger = _root(trust.ROLE_RESERVE)
    alien = trust.root_replacement(stranger, main.entry, stranger.entry, 1)
    with pytest.raises(trust.TrustError, match="не корнем"):
        trust.apply_root_replacement(anchors, alien)


def test_a_replacement_with_a_tampered_new_key_is_refused(org):
    main, reserve, _working, _cert, anchors = org
    doc = trust.root_replacement(reserve, main.entry, _root(trust.ROLE_MAIN).entry, 1)
    doc["replace"]["new"]["key"] = _root().entry["key"]
    with pytest.raises(trust.TrustError, match="подпись"):
        trust.apply_root_replacement(anchors, doc)
