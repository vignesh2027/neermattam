"""Neermattam MCP server: flood depth, water-aware routes and critical sites for any MCP client."""

from __future__ import annotations

import argparse


def main() -> None:
    import logging

    from .server import build_server

    # httpx logs every request at INFO; keep stderr for real problems.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(prog="neermattam-mcp", description="Neermattam MCP server")
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
