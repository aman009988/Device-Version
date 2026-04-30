"""
Storage type resolution — port of the fx_set_storage_type_using_serverid()
block inside feed.router.php#L6340-L6362.

PHP logic:
    if storage_type == "minio":  host = LIVEURL
    elif storage_type == "azure": host = "https://{account}.blob.core.windows.net"
    else (s3 default):            host = "https://s3-us-west-2.amazonaws.com"
    bucket = "/{awsBucketName}/"
"""
from __future__ import annotations

import config


def get_storage_type(server_id: int) -> str:
    """
    Returns "minio", "azure", or "s3" for the given server_id.
    Matches logic of fx_set_storage_type_using_serverid which reads
    SERVERS_USING_AZURE_STORAGE — a JSON map {"minio":[...], "azure":[...]}.
    Servers not listed default to "s3".
    """
    for storage_type, server_ids in config.SERVERS_USING_AZURE_STORAGE.items():
        if server_id in server_ids:
            return storage_type
    return "s3"


def resolve_host_and_bucket(server_id: int) -> tuple[str, str]:
    """
    Returns (host, bucket) strings exactly as PHP does before encrypting them.
    feed.router.php#L6341-L6359
    """
    storage_type = get_storage_type(server_id)

    if storage_type == "minio":
        host   = config.LIVEURL
        bucket = f"/{config.AWS_BUCKET_NAME}/"
    elif storage_type == "azure":
        host   = f"https://{config.AZURE_ACCOUNT_NAME}.blob.core.windows.net"
        bucket = f"/{config.AWS_BUCKET_NAME}/"
    else:
        # s3 default — feed.router.php#L6353
        host   = "https://s3-us-west-2.amazonaws.com"
        bucket = f"/{config.AWS_BUCKET_NAME}/"

    return host, bucket
