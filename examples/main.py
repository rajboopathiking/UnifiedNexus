import uvicorn
from pydantic import BaseModel, Field
from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("My Production App")

class UserLookup(BaseModel):
    user_id: int = Field(..., description="Numerical ID of the employee")

@nexus.universal_tool(path="/user-status")
def check_user_status(payload: UserLookup):
    """Retrieves the active status of a user."""
    return {"user_id": payload.user_id, "status": "Active", "role": "Admin"}

app = nexus.finalize()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="error")