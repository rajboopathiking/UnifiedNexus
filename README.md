# 🚀 Unified Nexus

**One Decorator. Two Interfaces. Total Focus.**

Unified Nexus is a high-performance Python framework designed to bridge the gap between traditional **REST APIs** and the new world of **LLM-native tools (MCP)**. It allows you to build product-grade backends where your web frontend and your AI agents share the exact same logic, schemas, and execution paths.

---

## ✨ Why Unified Nexus?

In traditional stacks, you often have to define a FastAPI route and then manually wrap it as an MCP tool. This leads to **Code Drift**, where the API and the Tool eventually go out of sync.

**Unified Nexus solves this by providing a Single Source of Truth:**
* **Speed:** Define logic once; it's instantly available via HTTP and SSE.
* **Consistency:** Shared Pydantic models ensure validation is identical for users and LLMs.
* **AI-Native:** Automatically generates rich tool descriptions for Claude, Cursor, and other MCP hosts using your Python docstrings.

---

## 🛠️ Quick Start

### 1. Installation
```bash
pip install -r requirements.txt
```

### 2. Create your Unified App (main.py)

```Python

from unified_nexus import UnifiedNexus
from pydantic import BaseModel, Field

nexus = UnifiedNexus("MyNexusAPI")

class UserRequest(BaseModel):
    user_id: int = Field(..., description="The unique ID of the user")

@nexus.universal_tool(path="/user-info")
def get_user(req: UserRequest):
    """
    Fetches user status and clearance level.
    This description is used by both Swagger and AI Agents!
    """
    return {"id": req.user_id, "status": "Active", "tier": "Gold"}

app = nexus.finalize()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

```

### 3. Run and Verify

**Start the server:**

```Bash
python main.py
```

### Web API Docs: http://localhost:8000/docs

### MCP SSE Endpoint: http://localhost:8000/sse

### 🚀 Novel Features

1)  Auto-Method Detection: Intelligently chooses GET or POST based on your function signature.

2) Shared Middleware: Apply auth or logging once; it protects both the API and the MCP interface.

**LLM-Optimized Errors:** Converts Python exceptions into natural language hints that help LLMs self-correct their tool calls.