# UnifiedNexus Documentation

Welcome to the official documentation for **UnifiedNexus v0.2.2** — the lightweight framework bridging FastAPI and FastMCP for AI-agent tool serving.

---

## 📚 Documentation Guides

- [🚀 Quickstart & Overview](../README.md): Installation, first universal tool, and core features.
- [🛡️ Enterprise Security Model](security.md): Dual-transport isolation, parameter validation, and credential protection.
- [🧩 Extensible Plugin Architecture](plugins.md): `NexusPlugin`, `NexusContext`, lifecycle hooks, auditing, and rate limiting.
- [🤖 AI-Agent Safety & Operational Invariants](safety-invariants.md): `ToolSafetyLevel`, human approvals, idempotency, bounds, and timeouts.
- [🚀 Production Deployment & Readiness](production-deployment.md): Shared responsibility model, Redis configuration, durable idempotency, and deployment checklist.
- [🏢 FastAPI Microservice Mounting](microservice-guide.md): Mounting onto existing apps, lifespan composition, and router inclusion.
- [🤝 LangServe Collaboration](langserve-collaboration.md): Running LangChain LCEL chains and FastMCP tools side-by-side.
- [📋 Changelog & Release Notes](../CHANGELOG.md): Version history and breaking changes.
