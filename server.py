"""
Fabric MCP Remote Server — Wrapper to expose ms-fabric-mcp-server over HTTP on Render.

This wraps the existing ms-fabric-mcp-server package with:
- Streamable HTTP transport (remote access via mcp-remote)
- Bearer token authentication (StaticTokenVerifier)
- CORS middleware (browser clients: SOFTIS Chat PAKAZURE)
- Health check endpoint for Render monitoring
"""

import os
import secrets
import logging

import uvicorn
from ms_fabric_mcp_server import create_fabric_server
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


def main():
    # 1. Create the Fabric MCP server using the package's factory function
    server = create_fabric_server(name="fabric-mcp-remote")

    # 2. Configure bearer token authentication
    api_key = os.environ.get("MCP_API_KEY")
    if not api_key:
        api_key = secrets.token_urlsafe(32)
        logger.warning(f"MCP_API_KEY not set, using generated key: {api_key}")

    auth = StaticTokenVerifier(
        tokens={
            api_key: {
                "client_id": "fabric-mcp-client",
                "scopes": ["fabric:all"],
            }
        }
    )
    server.auth = auth

    # 3. Add health check endpoint for Render
    @server.custom_route("/health", methods=["GET"])
    async def health_check(request):
        return JSONResponse({"status": "ok", "service": "fabric-mcp-remote"})

    # 4. CORS — requis pour les clients navigateur (SOFTIS Chat).
    #    Le middleware répond lui-même aux préflights OPTIONS, avant l'auth Bearer.
    cors = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id", "Accept"],
            expose_headers=["Mcp-Session-Id"],
            max_age=86400,
        )
    ]

    # 5. Run with Streamable HTTP transport on Render's PORT (uvicorn + ASGI app)
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("FASTMCP_HOST", "0.0.0.0")

    logger.info(f"Starting Fabric MCP Remote on {host}:{port} (CORS enabled)")

    app = server.http_app(path="/mcp", middleware=cors)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
