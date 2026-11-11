"""Signing keys. RS256: the server signs with a private key and publishes the public keys as a JWKS, so any client or
gateway can verify tokens without a shared secret. Old public keys stay in the JWKS while tokens signed with them live (rotation)."""
import base64
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.config import settings

log = logging.getLogger(__name__)


@dataclass
class KeyPair:
    kid: str
    private_pem: bytes
    public_key: rsa.RSAPublicKey


def _kid(public_key) -> str:
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()[:12]).decode().rstrip("=")


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode().rstrip("=")


def jwk_of(public_key) -> dict:
    nums = public_key.public_numbers()
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": _kid(public_key), "n": _b64(nums.n), "e": _b64(nums.e)}


class KeyStore:
    def __init__(self) -> None:
        pem = settings.jwt_private_key.replace("\n", "\n").encode() if settings.jwt_private_key else b""
        if not pem and settings.jwt_private_key_file:
            pem = Path(settings.jwt_private_key_file).read_bytes()
        if pem:
            private = serialization.load_pem_private_key(pem, password=None)
        else:
            if settings.env == "production":
                raise RuntimeError("MCP_JWT_PRIVATE_KEY (or MCP_JWT_PRIVATE_KEY_FILE) is required when MCP_ENV=production")
            log.warning("no JWT key configured: generated a throw-away key (tokens stop working on restart). Dev only.")
            private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.current = KeyPair(
            _kid(private.public_key()),
            private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
            private.public_key(),
        )
        self.public_keys: dict[str, rsa.RSAPublicKey] = {self.current.kid: self.current.public_key}
        for chunk in filter(None, (settings.jwt_extra_public_keys or "").split("|")):
            key = serialization.load_pem_public_key(chunk.replace("\n", "\n").encode())
            self.public_keys[_kid(key)] = key

    def jwks(self) -> dict:
        return {"keys": [jwk_of(k) for k in self.public_keys.values()]}


_store: KeyStore | None = None


def get_keys() -> KeyStore:
    global _store
    if _store is None:
        _store = KeyStore()
    return _store


def reset_keys() -> None:
    global _store
    _store = None
