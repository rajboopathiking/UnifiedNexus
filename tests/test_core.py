import pytest
from fastapi.testclient import TestClient
from unified_nexus import UnifiedNexus
from pydantic import BaseModel

nexus = UnifiedNexus("TestApp")

class MockData(BaseModel):
    id: int

@nexus.universal_tool(path="/test-endpoint")
def mock_function(data: MockData):
    """Test function."""
    return {"received": data.id}

app = nexus.finalize()
client = TestClient(app)

def test_api_routing():
    response = client.post("/test-endpoint", json={"id": 42})
    assert response.status_code == 200
    assert response.json()["received"] == 42

def test_mcp_registration():
    assert len(nexus._endpoints) == 1
    assert nexus._endpoints[0]["name"] == "mock_function"