"""Write the orchestrator's OpenAPI schema to a file, straight from the code.

    python scripts/export_openapi.py dashboard/openapi.json

The dashboard's typed client (dashboard/src/api/schema.gen.ts) is generated
from this schema. Generating it used to require a running orchestrator, so it
was left stale for months while the API grew; this needs only the source.
tests/test_api_client_current.py fails when the generated client misses a
route the API serves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.main import create_app  # noqa: E402


def main() -> None:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    destination.write_text(json.dumps(create_app().openapi(), indent=1), encoding="utf-8")
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
