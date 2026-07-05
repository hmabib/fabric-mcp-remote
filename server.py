"""
Fabric MCP Remote Server — Wrapper to expose ms-fabric-mcp-server over HTTP on Render.

This wraps the existing ms-fabric_mcp_server package with:
- Streamable HTTP transport (remote access via mcp-remote)
- Bearer token authentication (StaticTokenVerifier)
- CORS middleware (browser clients)
- Azure auth via refresh token with persistent rotation
- Health check endpoint for Render monitoring
"""

import os
import json
import time
import secrets
import logging
import threading

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

# Path to persist the rotated refresh token
TOKEN_CACHE_FILE = os.environ.get("TOKEN_CACHE_FILE", "/tmp/azure_refresh_token.json")


class RefreshTokenCredential:
    """Azure credential that uses a refresh token from az login.

    - Authenticates as the user (delegated permissions), no admin consent needed.
    - Persists rotated refresh tokens to disk so they survive server restarts.
    - Thread-safe: uses a lock around token acquisition.
    - Auto-refreshes 5 minutes before expiry.
    """

    def __init__(self, tenant_id: str, client_id: str, refresh_token: str):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._refresh_token = refresh_token
        self._lock = threading.Lock()
        self._cached_token = None  # (access_token, expires_on)
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._app = msal.PublicClientApplication(
            client_id=client_id, authority=authority
        )
        logger.info(f"RefreshTokenCredential initialized for tenant {tenant_id}")

    def _save_refresh_token(self, new_rt: str):
        """Persist the rotated refresh token to disk."""
        self._refresh_token = new_rt
        try:
            with open(TOKEN_CACHE_FILE, "w") as f:
                json.dump({"refresh_token": new_rt, "updated_at": time.time()}, f)
            logger.debug("Refresh token persisted to disk")
        except Exception as e:
            logger.warning(f"Failed to persist refresh token: {e}")

    def _load_refresh_token(self) -> str | None:
        """Load a previously persisted refresh token from disk."""
        try:
            if os.path.exists(TOKEN_CACHE_FILE):
                with open(TOKEN_CACHE_FILE) as f:
                    data = json.load(f)
                rt = data.get("refresh_token")
                if rt and len(rt) > 50:
                    logger.info("Loaded persisted refresh token from disk")
                    return rt
        except Exception as e:
            logger.warning(f"Failed to load persisted refresh token: {e}")
        return None

    def get_token(self, *scopes, **kwargs):
        """Acquire an access token. Uses cached token if still valid."""
        scope_list = list(scopes) if scopes else FABRIC_SCOPES

        with self._lock:
            # Return cached token if still valid (with 5 min buffer)
            if self._cached_token:
                token_str, expires_on = self._cached_token
                if time.time() < expires_on - 300:
                    return AccessToken(token_str, expires_on)

            # Try to load a persisted refresh token (might be newer than env var)
            persisted_rt = self._load_refresh_token()
            if persisted_rt:
                self._refresh_token = persisted_rt

            # Exchange refresh token for access token
            result = self._app.acquire_token_by_refresh_token(
                self._refresh_token, scopes=scope_list
            )

            if "error" in result:
                error_desc = result.get("error_description", result["error"])
                logger.error(f"Refresh token exchange failed: {error_desc[:200]}")
                raise Exception(f"Auth failed: {error_desc}")

            # Save the rotated refresh token (MSAL issues a new one each time)
            if "refresh_token" in result:
                self._save_refresh_token(result["refresh_token"])

            access_token = result["access_token"]
            expires_in = result.get("expires_in", 3600)
            expires_on = int(time.time()) + expires_in
            self._cached_token = (access_token, expires_on)

            logger.info(f"Access token acquired, expires in {expires_in}s")
            return AccessToken(access_token, expires_on)


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
        import ms_fabric_mcp_server.client.http_client as http_client

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
