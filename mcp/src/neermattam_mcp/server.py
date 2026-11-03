"""The MCP server: tools, one resource and one prompt over the live gateway."""

from __future__ import annotations

import json
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import tools
from .gateway import ADVISORY, Gateway

INSTRUCTIONS = f"""Neermattam measures street flood depth in seven Indian cities (Chennai, Mumbai, Bengaluru,
Hyderabad, Kolkata, Delhi, Visakhapatnam) from photos, fuses reports into a live depth map and plans
water-aware routes per vehicle. Rules for anything you tell a person from these tools:
- Every answer is advisory. Never tell anyone a road, route, cell or site is safe.
- A place with no recent report is unknown, not dry and not safe.
- Always give a depth with its uncertainty (sigma), and say when a depth is a model estimate, not a measurement.
- Coordinates are latitude, then longitude.
{ADVISORY}"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def build_server(gw: Gateway | None = None, allow_local_files: bool = True) -> MCPServer:
    """allow_local_files lets measure_photo read a path on this machine. It is
    on for stdio, where the client is the local user, and off for HTTP, where
    any caller could otherwise upload this machine's files."""
    gateway = gw
    mcp = MCPServer(name="neermattam", title="Neermattam flood depth", instructions=INSTRUCTIONS, version="0.1.0")

    def g() -> Gateway:
        nonlocal gateway
        if gateway is None:
            gateway = Gateway.from_env()
        return gateway

    @mcp.tool(annotations=READ_ONLY)
    async def list_cities() -> dict[str, Any]:
        """The seven covered cities with live rainfall (Open-Meteo), real reports in the last 24 hours,
        cells with evidence, the deepest known depth and the count of critical sites."""
        return await tools.list_cities(g())

    @mcp.tool(annotations=READ_ONLY)
    async def water_depth_near(
        lat: Annotated[float, Field(description="Latitude in degrees, such as 13.0418")],
        lon: Annotated[float, Field(description="Longitude in degrees, such as 80.2341")],
        radius_m: Annotated[float, Field(description="Search radius in metres, 50 to 5000", ge=50, le=5000)] = 500,
    ) -> dict[str, Any]:
        """How deep is the water near a point: the fused depth cells within radius_m, each with depth,
        sigma, freshness and hazard class. Streets with no evidence are unknown, never safe."""
        return await tools.water_depth_near(g(), lat, lon, radius_m)

    @mcp.tool(annotations=READ_ONLY)
    async def safe_route(
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        vehicle: Annotated[str, Field(description="walking, two_wheeler, auto, car or bus")] = "car",
        city: Annotated[str | None, Field(description="City id, optional; both ends must be in one city")] = None,
        include_geometry: Annotated[bool, Field(description="Also return the route's [lon, lat] points")] = False,
    ) -> dict[str, Any]:
        """A water-aware, advisory route for a vehicle inside one city: distance, time, cells avoided,
        unknown cells crossed, the deepest known water on it, and the shortest route ignoring water for
        comparison. A route is never a promise that the road is clear."""
        return await tools.safe_route(g(), from_lat, from_lon, to_lat, to_lon, vehicle, city, include_geometry)

    @mcp.tool(annotations=READ_ONLY)
    async def list_sites(
        city: Annotated[str, Field(description="City id, such as chennai")],
        kind: Annotated[str, Field(description="data_centre, hospital, telephone_exchange, substation or cable_landing_station")] = "data_centre",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Critical sites in a city with the status of their 300 m and 1 km watch rings, wettest first.
        Needs NEERMATTAM_API_KEY (the developer API)."""
        return await tools.list_sites(g(), city, kind, limit, offset)

    @mcp.tool(annotations=READ_ONLY)
    async def site_status(
        site_id: Annotated[str, Field(description="Site id, such as osm:node:12406588855")],
        include_route_points: bool = False,
    ) -> dict[str, Any]:
        """One critical site (data centre, hospital, exchange): its ring status, access routes for a
        diesel tanker, a two-wheeler and a car to a main road and a fuel station, and its area's flood
        record. Public for OpenStreetMap sites."""
        return await tools.site_status(g(), site_id, include_route_points)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def measure_photo(
        source: Annotated[str, Field(description="Local file path or http(s) URL of a JPEG/PNG photo or MP4/MOV video")],
        lat: Annotated[float, Field(description="Where the photo was taken: latitude")],
        lon: Annotated[float, Field(description="Where the photo was taken: longitude")],
        city: str | None = None,
        hint_reference: Annotated[str | None, Field(description="Optional: two_wheeler_wheel, car_wheel, auto_wheel, kerb or person")] = None,
    ) -> dict[str, Any]:
        """Submit a real flood photo taken at (lat, lon) to the live measurement service. It returns the
        depth with sigma, how it was obtained (measured or a labelled model estimate) and a link to its
        signed certificate. A usable real report is added to the live public map, so send only genuine
        photos of that place. The live demo has a daily cap; when it is reached nothing is measured."""
        return await tools.measure_photo(g(), source, lat, lon, city, hint_reference, allow_local_files)

    @mcp.tool(annotations=READ_ONLY)
    async def verify_certificate(
        measurement_id: Annotated[str, Field(description="A measurement id (m_...) or its /verify/ link")],
    ) -> dict[str, Any]:
        """Fetch a measurement's signed certificate and summarise it: whether the Ed25519 signature
        verifies against the published keys, the models and prompt versions, and the result."""
        return await tools.verify_certificate(g(), measurement_id)

    @mcp.resource("neermattam://cities", name="cities", title="Covered cities, live", mime_type="application/json")
    async def cities_resource() -> str:
        """The list_cities answer as a JSON resource."""
        return json.dumps(await tools.list_cities(g()), indent=1)

    @mcp.prompt(name="plan_trip_in_flooded_city", title="Plan a trip in a flooded city")
    def plan_trip(city: str, start: str, destination: str, vehicle: str = "car") -> str:
        """A step-by-step plan that checks the water before choosing a route."""
        return (
            f"I need to travel in {city} from {start} to {destination} by {vehicle}. Use the Neermattam tools:\n"
            "1. Call list_cities to see rainfall and how many real reports the city has in the last 24 hours.\n"
            "2. Turn the start and destination into latitude and longitude (ask me if you are not sure).\n"
            "3. Call water_depth_near at the start and at the destination.\n"
            f"4. Call safe_route with vehicle {vehicle}. Report distance, time, cells avoided, unknown cells crossed "
            "and the deepest known water, and compare with the shortest route.\n"
            "5. Tell me plainly what is known and what is unknown. Give every depth with its sigma. Never call the "
            "route or any street safe; unknown streets are unknown, not dry. End with the advisory line."
        )

    return mcp
