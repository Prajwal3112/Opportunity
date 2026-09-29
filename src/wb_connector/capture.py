"""Capture real World Bank API responses for deterministic regression tests."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import WorldBankApiClient
from .config import get_settings

CAPTURE_VERSION = 1
CAPTURE_DIRECTORY = Path("captures")


def _output_path(filename: str) -> Path:
    root = CAPTURE_DIRECTORY.resolve()
    path = (root / filename).resolve()
    if path.parent != root or path.suffix != ".json":
        raise ValueError("capture output must be a .json file directly within captures/")
    return path


async def capture(*, path: str, params: dict[str, Any], output: Path) -> None:
    async with WorldBankApiClient(get_settings()) as client:
        payload, request_url = await client.get_json(path, params)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "capture_version": CAPTURE_VERSION,
                "captured_at": datetime.now(UTC).isoformat(),
                "endpoint": path,
                "request_url": request_url,
                "request_params": params,
                "response": payload,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, choices=["projects", "documents", "procurement"])
    parser.add_argument("--output", required=True, help="Filename below captures/, for example wds-p165557.json")
    parser.add_argument("--projectid")
    parser.add_argument("--rows", type=int, default=10)
    parser.add_argument("--os", type=int, default=0)
    args = parser.parse_args()
    definitions = {
        "projects": ("/api/v2/projects", {"rows": args.rows}),
        "documents": ("/api/v3/wds", {"projectid": args.projectid, "rows": args.rows, "os": args.os}),
        "procurement": ("/api/procnotices", {"rows": args.rows, "os": args.os}),
    }
    path, params = definitions[args.endpoint]
    asyncio.run(capture(path=path, params=params, output=_output_path(args.output)))


if __name__ == "__main__":
    main()
