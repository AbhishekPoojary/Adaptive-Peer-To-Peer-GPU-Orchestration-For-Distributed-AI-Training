"""Pydantic request/response schemas for the orchestrator API.

Every inbound model forbids extra fields and rejects impossible values —
validation is a security boundary here, not a convenience.
"""
