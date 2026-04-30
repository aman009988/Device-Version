"""
RSA encryption — direct port of customFunctions.php#L38886
openSSlPasswordEncrypterForFeedRouter().

PHP original:
    $publicKey = file_get_contents('vendor/RSA_KEY/public_third_key.pem');
    openssl_public_encrypt($password, $encryptedPassword, $publicKey);
    return base64_encode($encryptedPassword);

PHP openssl_public_encrypt uses PKCS#1 v1.5 padding by default.
"""
from __future__ import annotations

import base64, os
import logging
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_public_key():
    """Load and cache the RSA public key from disk (same file PHP uses)."""
    try:
        if os.path.exists(config.RSA_PUBLIC_KEY_PATH):
            print(f"****************************Loading RSA public key from {config.RSA_PUBLIC_KEY_PATH}")
        else:
            print("**************************** RSA public key file not found at %s", config.RSA_PUBLIC_KEY_PATH)
        with open(config.RSA_PUBLIC_KEY_PATH, "rb") as f:
            return serialization.load_pem_public_key(f.read())
    except Exception as exc:
        logger.exception("Failed to load RSA public key from %s", config.RSA_PUBLIC_KEY_PATH)
        raise RuntimeError(f"RSA public key load failed: {exc}") from exc


def rsa_encrypt(value: str) -> str:
    """
    Encrypts `value` with the RSA public key using PKCS1v15 padding,
    then base64-encodes the result — identical to PHP openssl_public_encrypt.
    """
    public_key = _load_public_key()
    encrypted = public_key.encrypt(value.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")
