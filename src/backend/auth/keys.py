""" the backend agents signing key. the private half of the phase 3 keypair;
only this process many holf it. mcp server gets only public key"""

import base64
import os
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from backend.config import settings

class BackendAuthConfigurationError(RuntimeError):
    """ Raised at startup when the signing key is missing or malformed"""


@lru_cache(maxsize=1)
def get_private_key()-> Ed25519PrivateKey:
    raw=os.environ.get("MCP_PRIVATE_KEY") or settings.mcp_private_key
    if not raw:
        raise BackendAuthConfigurationError(
            "MCP_PRIVATE_KEY is not set. generate a keypair with scripts/generate_mcp_keys.py" \
            "and put mcp_private_key in .env"
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))
    except Exception as exc:
        raise BackendAuthConfigurationError(f"mcp_private_key is not valid base64 ed25519 bytes:{exc}") from exc

    