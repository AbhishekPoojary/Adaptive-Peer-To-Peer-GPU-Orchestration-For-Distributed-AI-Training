"""The dashboard's generated API client knows every route the API serves.

schema.gen.ts went stale once for months: five dashboard modules hand-wrote
fetch calls because the typed client predated their endpoints, and nothing
noticed. This fails as soon as an operation exists that the client lacks.
Regenerate with:

    python scripts/export_openapi.py dashboard/openapi.json
    cd dashboard && npx openapi-typescript openapi.json -o src/api/schema.gen.ts
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.main import create_app

_CLIENT = Path(__file__).resolve().parent.parent / "dashboard" / "src" / "api" / "schema.gen.ts"


def test_every_operation_is_in_the_generated_client() -> None:
    generated = _CLIENT.read_text(encoding="utf-8")
    missing = [
        f"{method.upper()} {path}"
        for path, operations in create_app().openapi()["paths"].items()
        for method, operation in operations.items()
        if f'{method}: operations["{operation["operationId"]}"]' not in generated
    ]
    assert not missing, (
        "the dashboard's API client is stale; regenerate it (see this module's "
        f"docstring). Missing: {missing}"
    )
