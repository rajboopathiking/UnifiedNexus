# LangServe & UnifiedNexus Collaboration

UnifiedNexus seamlessly coexists with [LangServe](https://github.com/langchain-ai/langserve), allowing teams to deploy **LangChain LCEL chains** alongside **FastMCP AI-agent tools** on a single unified FastAPI backend.

---

## 1. Why LangServe + UnifiedNexus?

- **LangServe** specializes in exposing LangChain Runnables and LCEL chains as streaming REST APIs (`/chain/invoke`, `/chain/stream`, `/chain/playground`).
- **UnifiedNexus** specializes in exposing Python business logic simultaneously as REST endpoints and FastMCP tools for autonomous agents (Claude, Cursor, LangGraph).

Combining them gives you an all-in-one AI platform:

```
                      ┌────────────────────────────────────────┐
                      │          FastAPI Application           │
                      │                                        │
                      │  ├── /rag/invoke (LangServe LCEL)      │
                      │  ├── /api/v1/tools (UnifiedNexus REST) │
                      │  └── /mcp (UnifiedNexus FastMCP SSE)   │
                      └────────────────────────────────────────┘
```

---

## 2. Complete Example

```python
from fastapi import FastAPI
from langserve import add_routes
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from unified_nexus import UnifiedNexus, ToolSafetyLevel

app = FastAPI(title="LangServe & UnifiedNexus Hybrid Backend")

# 1. Mount LangServe LCEL Chain:
prompt = ChatPromptTemplate.from_template("Summarize this text: {text}")
dummy_llm = RunnableLambda(lambda p: f"Summary of '{p.messages[0].content}'")
chain = prompt | dummy_llm

add_routes(app, chain, path="/summarize")

# 2. Attach UnifiedNexus AI-Agent Tools:
nexus = UnifiedNexus("AgentTools")

@nexus.universal_tool(path="/inventory", tool_type=ToolSafetyLevel.READ_ONLY)
def check_inventory(sku: str):
    """Checks warehouse inventory for a given SKU."""
    return {"sku": sku, "in_stock": True, "quantity": 42}

# Mount tools onto the shared FastAPI instance:
nexus.attach_to(app, api_prefix="/api/v1", mode="both")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

### Available Endpoints:
- 🔗 **LangServe REST Endpoint**: `http://localhost:8000/summarize/invoke`
- 🎮 **LangServe Playground**: `http://localhost:8000/summarize/playground/`
- 🌐 **UnifiedNexus REST Swagger**: `http://localhost:8000/docs`
- 🤖 **FastMCP Tool Server**: `http://localhost:8000/mcp`
