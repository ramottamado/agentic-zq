import pytest

from zero_query.agents.mcp_client import ImpalaTools
from zero_query.mcp_impala.server import build_server


@pytest.fixture
def tools() -> ImpalaTools:
    # In-process MCP connection: no ports, same tool code path as production.
    return ImpalaTools(build_server(max_rows=100))
