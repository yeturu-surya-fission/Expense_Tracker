"""One-time Ed25519 keypair generator for backend(MCP client) <-> mcp_server auth.

Generate once, paste both lines into .env. Re-running rotates the keypair:
every previously issued token stops verifying the moment the new public key
is loaded by the MCP server.
"""
import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

private = Ed25519PrivateKey.generate()
priv_bytes = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
pub_bytes = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

print("Add these two lines to your .env:")
print()
print(f"MCP_PRIVATE_KEY={base64.b64encode(priv_bytes).decode()}")
print(f"MCP_PUBLIC_KEY={base64.b64encode(pub_bytes).decode()}")
print()
print("MCP_PRIVATE_KEY is read ONLY by the backend (app/auth/keys.py) - it signs tokens.")
print("MCP_PUBLIC_KEY is read ONLY by the MCP server (mcp_server/auth/keys.py) - it verifies them.")
print("Re-running this script rotates the keypair: paste the new lines, restart both processes;")
print("all tokens issued before the rotation fail verification instantly.")
