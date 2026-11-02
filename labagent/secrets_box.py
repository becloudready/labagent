"""Secrets never leave labagent in plaintext: each is RSA-OAEP(SHA-256) encrypted to the orchestrator's public key.

The orchestrator (e.g. a Cloudflare Worker using WebCrypto RSA-OAEP, hash SHA-256) holds the private key.
"""
from __future__ import annotations

import base64
import secrets
import string

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def generate_password(length: int = 20) -> str:
    """Satisfies common IAM password policies: upper, lower, digit, symbol; no ambiguous characters."""
    upper, lower = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz"
    digits, symbols = "23456789", "!@#$%^&*-_=+"
    pools = [upper, lower, digits, symbols]
    chars = [secrets.choice(p) for p in pools]
    allchars = "".join(pools)
    chars += [secrets.choice(allchars) for _ in range(max(length, 12) - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def load_public_key(pem: str):
    return serialization.load_pem_public_key(pem.encode())


def encrypt(public_key, plaintext: str) -> str:
    ct = public_key.encrypt(
        plaintext.encode(),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ct).decode()


def load_private_key(pem: str):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def decrypt(private_key, ciphertext_b64: str) -> str:
    pt = private_key.decrypt(
        base64.b64decode(ciphertext_b64),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return pt.decode()


def generate_keypair():
    """A new RSA-3072 key pair as (private_pem, public_pem). The private key is unencrypted: keep it off shared disks."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public
