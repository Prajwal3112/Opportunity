import os

import pytest

from wb_connector.client import WorldBankApiClient
from wb_connector.config import get_settings
from wb_connector.connectors import DocumentsConnector, ProcurementConnector, ProjectsConnector

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def live_tests_are_opt_in() -> None:
    if os.getenv("RUN_LIVE_WORLD_BANK_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_WORLD_BANK_TESTS=1 to contact live World Bank services")


@pytest.mark.asyncio
async def test_projects_documents_and_procurement_live_contract() -> None:
    async with WorldBankApiClient(get_settings()) as client:
        project = await anext(ProjectsConnector(client).discover(rows=1))
        assert project.project_id
        documents = DocumentsConnector(client)
        # This intentionally makes no assertion that every project has documents.
        async for document in documents.for_project(project.project_id, rows=1):
            assert document.external_id
            assert document.project_id == project.project_id
            break
        async for notice in ProcurementConnector(client).discover(rows=1):
            assert notice.external_id
            break
