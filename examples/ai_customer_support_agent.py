"""
ai_customer_support_agent.py — Enterprise AI Customer Support Agent Proof-of-Concept

Demonstrates:
- UnifiedNexus serving tools to autonomous AI agents over Model Context Protocol (MCP) and REST.
- Scenario 1: Search Knowledge — Read-only policy/documentation search.
- Scenario 2: Check Order — Tenant and caller isolation (customer can only query their own orders).
- Scenario 3: Create Ticket — Action tool with Pydantic validation, idempotency, and audit plugin logging.
- Running as a single FastAPI microservice with /chat endpoint and /mcp tool server.
"""

import os
import sys
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from unified_nexus import (
    AuthorizationError,
    NexusContext,
    NexusPlugin,
    ToolSafetyLevel,
    UnifiedNexus,
)

# 1. Domain Data Stores
KNOWLEDGE_BASE = {
    "return_policy": "Full refunds offered within 30 days of purchase in original packaging.",
    "warranty": "Hardware includes a 1-year limited warranty covering manufacturing defects.",
    "shipping": "Standard delivery takes 3 to 5 business days.",
}

ORDERS = {
    "ORD-101": {"order_id": "ORD-101", "customer_id": "cust_alice", "status": "Delivered", "items": ["Laptop Stand"]},
    "ORD-202": {"order_id": "ORD-202", "customer_id": "cust_bob", "status": "In Transit", "items": ["USB-C Hub"]},
}

TICKETS = {}


# 2. Extensible Compliance & Audit Plugin
class SupportAuditPlugin(NexusPlugin):
    async def startup(self, nexus: Any) -> None:
        print("🛡️  [Audit Plugin] Initialized compliance tracing for Customer Support Agent.")

    async def before_execute(self, context: NexusContext, args: Dict[str, Any]) -> None:
        print(f"🔍 [Audit] Incoming {context.transport.upper()} call to '{context.tool_name}' (ID: {context.request_id[:8]})")

    async def after_execute(self, context: NexusContext, result: Any) -> Any:
        print(f"✅ [Audit] Tool '{context.tool_name}' completed successfully in {context.duration_ms:.2f}ms")
        return result

    async def on_error(self, context: NexusContext, error: Exception) -> None:
        print(f"🚨 [Audit] Security/Execution rejection in '{context.tool_name}': {error}")


# 3. Configure UnifiedNexus
nexus = UnifiedNexus("CustomerSupportAgentService", version="0.2.0")
nexus.add_plugin(SupportAuditPlugin())


# ── Scenario 1: Knowledge Search (Safe Read-Only) ───────────────────────────
@nexus.universal_tool(path="/knowledge/search", tool_type=ToolSafetyLevel.READ_ONLY, idempotent=True)
def search_knowledge(topic: str) -> dict:
    """Searches company knowledge base articles for policies and FAQ information."""
    key = topic.lower().strip()
    article = KNOWLEDGE_BASE.get(key, "Article not found. Please escalate inquiry to human support.")
    return {"topic": key, "article": article}


# ── Scenario 2: Check Order (Tenant & Customer Identity Isolation) ──────────
@nexus.universal_tool(path="/orders/check", tool_type=ToolSafetyLevel.READ_ONLY)
def check_order(order_id: str, caller_id: str) -> dict:
    """
    Checks tracking status for a specific customer order.
    Enforces customer ownership: caller_id must match order owner.
    """
    order = ORDERS.get(order_id)
    if not order:
        raise ValueError(f"Order '{order_id}' was not found in system.")

    if order["customer_id"] != caller_id and caller_id != "admin_agent":
        raise AuthorizationError(
            f"Unauthorized: Caller '{caller_id}' cannot inspect order '{order_id}' "
            f"belonging to another customer."
        )

    return order


# ── Scenario 3: Create Support Ticket (State-Changing with Idempotency) ─────
class TicketPayload(BaseModel):
    customer_id: str = Field(..., description="Customer account identifier")
    subject: str = Field(..., min_length=3, max_length=100, description="Brief title of issue")
    description: str = Field(..., min_length=5, description="Full details of customer issue")
    idempotency_key: Optional[str] = Field(None, description="Unique key for safe retries")


@nexus.universal_tool(
    path="/tickets/create",
    tool_type=ToolSafetyLevel.ACTION,
    idempotent=True,
)
def create_ticket(ticket: TicketPayload) -> dict:
    """Creates a support ticket with input validation and idempotent retries."""

    # Idempotent deduplication check
    if ticket.idempotency_key and ticket.idempotency_key in TICKETS:
        return {"status": "existing", "ticket": TICKETS[ticket.idempotency_key]}

    ticket_id = f"TCK-{len(TICKETS) + 1001}"
    record = {
        "ticket_id": ticket_id,
        "customer_id": ticket.customer_id,
        "subject": ticket.subject,
        "description": ticket.description,
        "status": "OPEN",
    }

    key = ticket.idempotency_key or ticket_id
    TICKETS[key] = record
    return {"status": "created", "ticket": record}


# 4. Agent Chat Integration (Simulating LangGraph / AI Agent Interface)
app = nexus.finalize()

class ChatRequest(BaseModel):
    user_id: str
    message: str


@app.post("/chat")
async def chat_with_agent(req: ChatRequest):
    """
    Conversational agent endpoint.
    Agents like LangGraph or Claude discover and invoke UnifiedNexus tools via MCP (/mcp).
    """
    # Example agent dispatch: agent uses search_knowledge or check_order safely
    if "return" in req.message.lower():
        result = search_knowledge("return_policy")
        return {"agent_reply": f"Our return policy: {result['article']}"}
    elif "order" in req.message.lower() and "ORD-" in req.message:
        words = req.message.split()
        order_id = next((w for w in words if w.startswith("ORD-")), "ORD-101")
        try:
            order_info = check_order(order_id, caller_id=req.user_id)
            return {"agent_reply": f"Order {order_id} is currently {order_info['status']}."}
        except AuthorizationError as ae:
            raise HTTPException(status_code=403, detail=str(ae))

    return {"agent_reply": "I am here to assist with company policies, orders, or support tickets."}


if __name__ == "__main__":
    print("🚀 Starting Customer Support Agent Server...")
    print("🌐 REST Chat & API: http://127.0.0.1:8000/docs")
    print("🤖 MCP Protocol:    http://127.0.0.1:8000/mcp")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
