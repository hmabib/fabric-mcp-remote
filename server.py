"""
Fabric MCP Remote Server — Wrapper to expose ms-fabric-mcp-server over HTTP on Render.

This wraps the existing ms-fabric_mcp_server package with:
- Streamable HTTP transport (remote access via mcp-remote)
- Bearer token authentication (StaticTokenVerifier)
- CORS middleware (browser clients)
- Azure auth via refresh token (no admin consent needed)
- Health check endpoint for Render monitoring
"""

import os
import secrets
import logging

import uvicorn
import msal
from ms_fabric_mcp_server import create_fabric_server
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from azure.core.credentials import AccessToken

logger = logging.getLogger(__name__)

# Azure CLI public client ID (from az login session)
AZURE_CLI_CLIENT_ID = os.environ.get("AZURE_CLI_CLIENT_ID", "04b07795-8ddb-461a-bbee-02f9e1bf7b46")
FABRIC_SCOPES = ["https://api.fabric.microsoft.com/.default"]
POWERBI_SCOPES = ["https://analysis.windows.net/powerbi/api/.default"]


class RefreshTokenCredential:
    """Azure credential that uses a refresh token from az login.

    This authenticates as the user (delegated permissions) instead of a
    service principal (application permissions), so no admin consent is needed.
    The refresh token is automatically rotated by MSAL.
    """

    def __init__(self, tenant_id: str, client_id: str, refresh_token: str):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._refresh_token = refresh_token
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._app = msal.PublicClientApplication(
            client_id=client_id, authority=authority
        )
        logger.info(f"RefreshTokenCredential initialized for tenant {tenant_id}")

    def get_token(self, *scopes, **kwargs):
        """Acquire an access token using the refresh token."""
        scope_list = list(scopes) if scopes else FABRIC_SCOPES

        # Try the cached refresh token
        result = self._app.acquire_token_by_refresh_token(
            self._refresh_token, scopes=scope_list
        )

        if "error" in result:
            logger.warning(
                f"Refresh token exchange failed: {result.get('error_description', result['error'])}"
            )
            raise Exception(f"Auth failed: {result.get('error_description', result['error'])}")

        # Save the new refresh token for next call (MSAL rotates them)
        if "refresh_token" in result:
            self._refresh_token = result["refresh_token"]
            logger.debug("Refresh token rotated successfully")

        access_token = result["access_token"]
        expires_in = result.get("expires_in", 3600)
        import time
        return AccessToken(access_token, int(time.time()) + expires_in)


def main():
    # 0. Set up Azure authentication via refresh token
    tenant_id = os.environ.get("AZURE_TENANT_ID", "")
    refresh_token = os.environ.get("AZURE_REFRESH_TOKEN", "")

    if refresh_token and tenant_id:
        # Use refresh token credential (delegated permissions, no admin consent needed)
        credential = RefreshTokenCredential(
            tenant_id=tenant_id,
            client_id=AZURE_CLI_CLIENT_ID,
            refresh_token=refresh_token,
        )
        logger.info("Using RefreshTokenCredential (delegated/user auth)")

        # Monkey-patch DefaultAzureCredential in the Fabric client
        # so that FabricClient._setup_credential uses our credential instead
        import ms_fabric_mcp_server.client.http_client as http_client
        original_setup = http_client.FabricClient._setup_credential

        def patched_setup(self_inner):
            self_inner._credential = credential
            logger.debug("FabricClient using RefreshTokenCredential")

        http_client.FabricClient._setup_credential = patched_setup

        # Also patch the SQL service if present
        try:
            import ms_fabric_mcp_server.services.sql as sql_svc
            sql_svc.DefaultAzureCredential = lambda: credential
        except (ImportError, AttributeError):
            pass
    else:
        # Fallback: map FABRIC_* env vars to AZURE_* for DefaultAzureCredential
        if os.environ.get("FABRIC_TENANT_ID") and not os.environ.get("AZURE_TENANT_ID"):
            os.environ["AZURE_TENANT_ID"] = os.environ["FABRIC_TENANT_ID"]
        if os.environ.get("FABRIC_CLIENT_ID") and not os.environ.get("AZURE_CLIENT_ID"):
            os.environ["AZURE_CLIENT_ID"] = os.environ["FABRIC_CLIENT_ID"]
        if os.environ.get("FABRIC_CLIENT_SECRET") and not os.environ.get("AZURE_CLIENT_SECRET"):
            os.environ["AZURE_CLIENT_SECRET"] = os.environ["FABRIC_CLIENT_SECRET"]
        logger.info("Using DefaultAzureCredential (service principal auth)")

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

    # 4. CORS middleware — required for browser clients
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

    # 5. Run with Streamable HTTP transport on Render's PORT
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("FASTMCP_HOST", "0.0.0.0")

    logger.info(f"Starting Fabric MCP Remote on {host}:{port} (CORS enabled)")

    app = server.http_app(path="/mcp", middleware=cors)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
