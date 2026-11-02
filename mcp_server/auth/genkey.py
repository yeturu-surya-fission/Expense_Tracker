"""Print a new RSA signing key (PEM) for MCP_JWT_PRIVATE_KEY:   python -m mcp_server.auth.genkey > secrets/jwt_private.pem"""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

if __name__ == "__main__":
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    print(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), end="")
