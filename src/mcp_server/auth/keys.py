""" the mcp servers verifying key. only the public key lives here by design:
this process can check tokens but can never mint one"""

import base64
import os
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from backend.config import settings

TOKEN_ALG="EdDSA" #must match what the backend signs with

class MCPServerConfigurationError(RuntimeError):
    """ Raised at the startup when the key material is missing or malformed."""

@lru_cache(maxsize=1)
def get_public_key()-> Ed25519PublicKey:
    raw=os.environ.get("MCP_PUBLIC_KEY") or settings.mcp_public_key
    if not raw:
        raise MCPServerConfigurationError(
            "MCP_PUBLIC_KEY is no set. generate a keypair with scripts/generate_mcp_keys.py"
            "and put MCP_PUBLIC_KEY in .env"
        )
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(raw))
    except Exception as exc:
        raise MCPServerConfigurationError(f"MCP_PUBLIC_KEY is not valid base64 Ed25519 bytes: {exc}") from exc
