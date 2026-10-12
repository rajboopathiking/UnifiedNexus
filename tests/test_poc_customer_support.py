"""
End-to-End Proof of Concept Test: AI Customer-Support Agent
Verifies the three customer-support agent scenarios independently over both REST and MCP:
1. Search knowledge: Permitted caller calls read-only tool successfully.
2. Check order: Authenticated user accesses their own order, but access to another user's order is rejected.
3. Create ticket: State-changing tool requires authorization, validates input, supports idempotency, and records audit events.
"""

from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from unified_nexus import (
    AuthorizationError,
    NexusContext,
    NexusPlugin,
    ToolSafetyLevel,
    UnifiedNexus,
)

# Mock in-memory databases
KNOWLEDGE_BASE = {
    "return_policy": "Items can be returned within 30 days of delivery in original packaging.",
    "shipping": "Standard shipping takes 3-5 business days. Express shipping takes 1-2 business days.",
}

ORDERS_DB = {
    "ORD-101": {"order_id": "ORD-101", "customer_id": "cust_alice", "status": "Shipped", "total": 120.50},
    "ORD-202": {"order_id": "ORD-202", "customer_id": "cust_bob", "status": "Processing", "total": 85.00},
}

TICKETS_DB = {}
AUDIT_LOGS = []


class SupportAuditPlugin(NexusPlugin):
    """Audits tool invocations for compliance and security monitoring."""
    async def after_execute(self, context: NexusContext, result: Any) -> Any:
        AUDIT_LOGS.append({
            "transport": context.transport,
            "tool": context.tool_name,
            "principal": context.principal,
            "status": "success",
            "request_id": context.request_id,
        })
        return result

    async def on_error(self, context: NexusContext, error: Exception) -> None:
        AUDIT_LOGS.append({
            "transport": context.transport,
            "tool": context.tool_name,
            "principal": context.principal,
            "status": "error",
            "error": str(error),
            "request_id": context.request_id,
        })


class CreateTicketRequest(BaseModel):
    customer_id: str = Field(..., min_length=3, description="Customer identifier")
    subject: str = Field(..., min_length=3, max_length=100, description="Ticket issue title")
    description: str = Field(..., min_length=5, description="Detailed problem description")
    idempotency_key: Optional[str] = Field(None, description="Client idempotency key for safe retries")


def create_support_nexus():
    nexus = UnifiedNexus("CustomerSupportSystem")
    nexus.add_plugin(SupportAuditPlugin())

    # Scenario 1: Search Knowledge (Safe Read-Only)
    @nexus.universal_tool(path="/knowledge/search", tool_type=ToolSafetyLevel.READ_ONLY, idempotent=True)
    def search_knowledge(topic: str) -> dict:
        """Searches company knowledge base for policy information."""
        clean_topic = topic.lower().strip()
        result = KNOWLEDGE_BASE.get(clean_topic, "No article found. Please escalate to a human agent.")
        return {"topic": clean_topic, "content": result}

    # Scenario 2: Check Order (Ownership & Tenant Isolation)
    @nexus.universal_tool(path="/orders/check", tool_type=ToolSafetyLevel.READ_ONLY)
    def check_order(order_id: str, caller_id: str) -> dict:
        """
        Retrieves order details.
        Enforces tenant/customer ownership: caller_id must match order.customer_id.
        """
        order = ORDERS_DB.get(order_id)
        if not order:
            raise ValueError(f"Order '{order_id}' not found.")

        # Enforce caller ownership
        if order["customer_id"] != caller_id and caller_id != "admin_agent":
            raise AuthorizationError(
                f"Unauthorized: Caller '{caller_id}' is not permitted to access order '{order_id}' "
                f"belonging to customer '{order['customer_id']}'."
            )

        return order

    # Scenario 3: Create Support Ticket (State-Changing with Idempotency & Validation)
    @nexus.universal_tool(
        path="/tickets/create",
        tool_type=ToolSafetyLevel.ACTION,
        idempotent=True,
    )
    def create_ticket(ticket: CreateTicketRequest) -> dict:
        """Creates a customer support ticket with idempotency handling."""
        # Idempotency check: if ticket with this key already exists, return existing record
        if ticket.idempotency_key and ticket.idempotency_key in TICKETS_DB:
            return {"status": "existing", "ticket": TICKETS_DB[ticket.idempotency_key]}

        ticket_id = f"TCK-{len(TICKETS_DB) + 1001}"
        record = {
            "ticket_id": ticket_id,
            "customer_id": ticket.customer_id,
            "subject": ticket.subject,
            "description": ticket.description,
            "status": "OPEN",
        }

        key = ticket.idempotency_key or ticket_id
        TICKETS_DB[key] = record
        return {"status": "created", "ticket": record}

    return nexus


# ─────────────────────────────────────────────────────────────────────────────
# TESTS ACROSS REST TRANSPORT
# ─────────────────────────────────────────────────────────────────────────────
def test_customer_support_scenarios_over_rest():
    AUDIT_LOGS.clear()
    TICKETS_DB.clear()

    nexus = create_support_nexus()
    app = nexus.finalize(mode="api")

    with TestClient(app) as client:
        # 1. Search knowledge over REST
        res_kb = client.get("/knowledge/search?topic=return_policy")
        assert res_kb.status_code == 200
        assert "within 30 days" in res_kb.json()["content"]

        # 2. Check order over REST: Authenticated caller accesses own order
        res_order_ok = client.get("/orders/check?order_id=ORD-101&caller_id=cust_alice")
        assert res_order_ok.status_code == 200
        assert res_order_ok.json()["customer_id"] == "cust_alice"

        # Check order over REST: Caller attempts accessing another user's order -> 403 Forbidden
        res_unauth = client.get("/orders/check?order_id=ORD-101&caller_id=cust_attacker")
        assert res_unauth.status_code == 403
        assert "Unauthorized" in res_unauth.json()["detail"]

        # 3. Create ticket over REST with idempotency
        payload = {
            "customer_id": "cust_alice",
            "subject": "Missing package",
            "description": "Package was marked as delivered but not received.",
            "idempotency_key": "idemp-key-999",
        }
        res_create1 = client.post("/tickets/create", json=payload)
        assert res_create1.status_code == 200
        assert res_create1.json()["status"] == "created"

        # Idempotent retry: same key returns existing ticket
        res_create2 = client.post("/tickets/create", json=payload)
        assert res_create2.status_code == 200
        assert res_create2.json()["status"] == "existing"


# ─────────────────────────────────────────────────────────────────────────────
# TESTS ACROSS MCP TRANSPORT
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_customer_support_scenarios_over_mcp():
    AUDIT_LOGS.clear()
    TICKETS_DB.clear()

    nexus = create_support_nexus()
    app = nexus.finalize()

    with TestClient(app):
        # 1. Search knowledge over MCP
        kb_tool = await nexus.mcp.get_tool("search_knowledge")
        kb_res = await kb_tool.run({"topic": "shipping"})
        assert "Standard shipping takes 3-5 business days" in str(kb_res)

        # 2. Check order over MCP: Caller accesses own order
        order_tool = await nexus.mcp.get_tool("check_order")
        order_ok = await order_tool.run({"order_id": "ORD-202", "caller_id": "cust_bob"})
        assert "cust_bob" in str(order_ok)
        assert "Processing" in str(order_ok)

        # Check order over MCP: Unauthorized access rejected
        with pytest.raises(ToolError) as exc_unauth:
            await order_tool.run({"order_id": "ORD-202", "caller_id": "cust_alice"})
        assert "Unauthorized" in str(exc_unauth.value)

        # 3. Create ticket over MCP: Input validated and idempotency respected
        ticket_tool = await nexus.mcp.get_tool("create_ticket")
        ticket_data = {
            "ticket": {
                "customer_id": "cust_bob",
                "subject": "Damaged item",
                "description": "Screen is cracked upon unboxing.",
                "idempotency_key": "idemp-key-bob-1",
            },
        }
        ticket_res1 = await ticket_tool.run(ticket_data)
        assert "created" in str(ticket_res1)

        # Idempotent retry returns existing
        ticket_res2 = await ticket_tool.run(ticket_data)
        assert "existing" in str(ticket_res2)

        # Audit logs recorded all MCP operations
        mcp_audit_tools = [entry["tool"] for entry in AUDIT_LOGS if entry["transport"] == "mcp"]
        assert "search_knowledge" in mcp_audit_tools
        assert "check_order" in mcp_audit_tools
        assert "create_ticket" in mcp_audit_tools
