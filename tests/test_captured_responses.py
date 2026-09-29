"""Regression validation for checked-in captures that originate from real requests."""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wb_connector.client import WorldBankApiClient
from wb_connector.connectors import ProcurementConnector

CAPTURES = sorted(Path("captures").glob("*.json"))


@pytest.mark.parametrize("capture_path", CAPTURES, ids=lambda path: path.stem)
def test_capture_has_provenance_and_parseable_response(capture_path: Path) -> None:
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    assert capture["capture_version"] == 1
    assert capture["endpoint"].startswith("/api/")
    assert isinstance(capture["request_params"], dict)
    assert isinstance(capture["request_url"], str)
    assert isinstance(capture["response"], dict)
    record_key = "documents" if capture["endpoint"] == "/api/v3/wds" else "procnotices"
    WorldBankApiClient.page_from_payload(capture["response"], record_key)


@pytest.mark.parametrize(
    "capture_path",
    [path for path in CAPTURES if json.loads(path.read_text(encoding="utf-8"))["endpoint"] == "/api/procnotices"],
    ids=lambda path: path.stem,
)
def test_procurement_capture_uses_the_observed_envelope_and_preserves_raw_records(
    capture_path: Path,
) -> None:
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    response = capture["response"]
    assert {"rows", "os", "page", "total", "procnotices"}.issubset(response)
    assert isinstance(response["procnotices"], list)
    for raw_record in response["procnotices"]:
        notice = ProcurementConnector.normalize_record(
            raw_record, source_url=capture["request_url"], retrieved_at=datetime.now(UTC)
        )
        if notice is None:
            continue
        assert notice.external_id == raw_record["id"]
        assert notice.raw_payload == raw_record
        assert notice.notice_text == raw_record.get("notice_text")
        assert notice.bid_description == raw_record.get("bid_description")
        assert notice.notice_type == raw_record.get("notice_type")
        assert notice.procurement_method_code == raw_record.get("procurement_method_code")
        assert notice.procurement_method_name == raw_record.get("procurement_method_name")


if not CAPTURES:
    pytestmark = pytest.mark.skip(reason="No real World Bank response captures are checked in")
