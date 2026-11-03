"""Live smoke test, not run in CI: starts the server over stdio, lists its
tools and calls them against the live gateway (NEERMATTAM_URL or the
default). It never calls measure_photo, so it spends none of the daily
measurement cap and adds nothing to the live map.

    uv run python scripts/live_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

CHENNAI_FROM = (13.0418, 80.2341)  # T. Nagar
CHENNAI_TO = (13.0067, 80.2206)  # Guindy


def show(title: str, result) -> None:
    print(f"\n== {title} (is_error={result.is_error})")
    if result.structured_content is not None:
        print(json.dumps(result.structured_content, indent=1, ensure_ascii=False))
    else:
        print(result.content[0].text)


async def main() -> None:
    env = {"NEERMATTAM_URL": os.environ.get("NEERMATTAM_URL", "")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "neermattam_mcp"], env=env)
    async with Client(params, read_timeout_seconds=180) as c:
        tools = (await c.list_tools()).tools
        print("tools over stdio:", ", ".join(t.name for t in tools))
        print("prompts:", ", ".join(p.name for p in (await c.list_prompts()).prompts))
        show("list_cities", await c.call_tool("list_cities", {}))
        show("safe_route car, T. Nagar to Guindy", await c.call_tool("safe_route", {
            "from_lat": CHENNAI_FROM[0], "from_lon": CHENNAI_FROM[1], "to_lat": CHENNAI_TO[0], "to_lon": CHENNAI_TO[1], "vehicle": "car"}))
        show("water_depth_near T. Nagar, 500 m", await c.call_tool("water_depth_near", {"lat": CHENNAI_FROM[0], "lon": CHENNAI_FROM[1], "radius_m": 500}))
        show("site_status osm:node:12406588855", await c.call_tool("site_status", {"site_id": "osm:node:12406588855"}))
        show("verify_certificate m_01M6C1RE0T43R0YKRCE6Q51DHA", await c.call_tool("verify_certificate", {"measurement_id": "m_01M6C1RE0T43R0YKRCE6Q51DHA"}))
        show("safe_route across two cities (error path)", await c.call_tool("safe_route", {
            "from_lat": 13.0418, "from_lon": 80.2341, "to_lat": 19.1, "to_lon": 72.95, "vehicle": "car"}))


if __name__ == "__main__":
    asyncio.run(main())
