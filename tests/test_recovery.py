import base64

import pytest

from portable_os_admin import crypto
from portable_os_admin.container import ContainerManager, MediaRecord, RecoveryRecord
from portable_os_admin.errors import MediaNotRegisteredError, RecoveryDeniedError, RecoveryRequestInvalidError
from portable_os_admin.recovery import RecoveryRequest, RecoveryService
from portable_os_admin.session import AdminSession


@pytest.fixture
def session_with_media(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    session = AdminSession(container)
    media_id = crypto.new_media_id()
    container.add_media_record(
        MediaRecord(media_id=media_id, luks_uuid="u-1", label="stick", config_name="cfg", target_os="debian")
    )
    code = crypto.generate_recovery_code()
    salt = crypto.new_salt()
    container.add_recovery_record(
        RecoveryRecord(media_id=media_id, slot=7, code=code, salt_b64=base64.b64encode(salt).decode())
    )
    return session, media_id, code


def test_request_encode_parse_round_trip():
    request = RecoveryRequest(media_id="abcd1234", slot=3)
    wire = request.encode()
    parsed = RecoveryRequest.parse(wire)
    assert parsed == request


def test_request_parse_rejects_bad_checksum():
    request = RecoveryRequest(media_id="abcd1234", slot=3)
    wire = request.encode()
    tampered = wire[:-1] + ("0" if wire[-1] != "0" else "1")
    with pytest.raises(RecoveryRequestInvalidError):
        RecoveryRequest.parse(tampered)


@pytest.mark.parametrize("bad_text", ["not-a-request", "a:b:c:d", "media:notanumber:00000000"])
def test_request_parse_rejects_malformed_text(bad_text):
    with pytest.raises(RecoveryRequestInvalidError):
        RecoveryRequest.parse(bad_text)


def test_issue_code_success(session_with_media):
    session, media_id, code = session_with_media
    request = RecoveryRequest(media_id=media_id, slot=7).encode()
    issued = RecoveryService(session).issue_code(request)
    assert issued == code
    record = session.container.find_recovery(media_id, 7)
    assert record.status == "used"
    assert record.used_at is not None


def test_issue_code_twice_is_denied(session_with_media):
    session, media_id, _ = session_with_media
    request = RecoveryRequest(media_id=media_id, slot=7).encode()
    service = RecoveryService(session)
    service.issue_code(request)
    with pytest.raises(RecoveryDeniedError):
        service.issue_code(request)


def test_issue_code_unknown_media(session_with_media):
    session, _, _ = session_with_media
    request = RecoveryRequest(media_id="unknownid", slot=7).encode()
    with pytest.raises(MediaNotRegisteredError):
        RecoveryService(session).issue_code(request)


def test_issue_code_unknown_slot(session_with_media):
    session, media_id, _ = session_with_media
    request = RecoveryRequest(media_id=media_id, slot=99).encode()
    with pytest.raises(RecoveryDeniedError):
        RecoveryService(session).issue_code(request)
