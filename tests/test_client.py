import httpx
import pytest

from wb_connector.client import WorldBankApiClient
from wb_connector.config import Settings
from wb_connector.errors import WorldBankPayloadError, WorldBankResponseError


def settings() -> Settings:
    return Settings(world_bank_base_url="https://example.invalid", world_bank_max_retries=1)


@pytest.mark.asyncio
async def test_request_includes_json_format_without_claiming_a_world_bank_payload() -> None:
    observed = {}
    async def handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.url.params)
        return httpx.Response(500, request=request)
    async with WorldBankApiClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WorldBankResponseError):
            await client.get_json("/api/v2/projects", {"rows": 2})
    assert observed == {"format": "json", "rows": "2"}


def test_page_rejects_non_mapping_records() -> None:
    with pytest.raises(WorldBankPayloadError):
        WorldBankApiClient.page_from_payload({"projects": ["not a record"]}, "projects")
