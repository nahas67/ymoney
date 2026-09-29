"""S3 object-storage connector (Work 10 Lane C).

Honest scope: lists objects under an optional prefix in the configured
bucket — metadata first, content only for ``text/*`` keys (capped at
``MAX_SOURCE_BYTES``). Credentials/endpoint/region come from operator
settings (``s3_bucket``, ``s3_endpoint_url``, ``s3_access_key``,
``s3_secret_key``, ``s3_region``); the connector config may override the
bucket and set ``prefix``. ``allow_private`` is accepted for interface
uniformity but unused: the endpoint is operator settings, not connector
input, so it is trusted by design.

``health()`` never fakes availability: no configured bucket or a missing
boto3 import surfaces as UNAVAILABLE with the real reason (via
``connect()`` raising SourceConfigError).

Pagination: ``list_objects_v2`` page size 100, ``ContinuationToken`` in,
``NextContinuationToken`` out (empty string = final page), so the sync
layer's completed-pass rule governs snapshot deletions.
"""

from __future__ import annotations

import mimetypes
from datetime import UTC

from app.core.config import settings
from app.engine.sources.base import (
    MAX_SOURCE_BYTES,
    SourceConfigError,
    SourceConnector,
    SourceDocumentDoc,
    SourceError,
    canonical_remote_id,
)

_PAGE_SIZE = 100
# placeholders some providers return instead of a real content type
_OCTET_TYPES = ("", "binary/octet-stream", "application/octet-stream")


def _mime_for(key: str, content_type: str | None) -> str:
    """Content-Type first, extension guess as the honest fallback."""
    explicit = str(content_type or "").split(";")[0].strip().lower()
    if explicit and explicit not in _OCTET_TYPES:
        return explicit
    guessed, _ = mimetypes.guess_type(key)
    return guessed or explicit or "application/octet-stream"


def _is_text(mime: str) -> bool:
    return mime.startswith("text/")


class S3Connector(SourceConnector):
    """Lists one page of S3 objects under a prefix (snapshot lists)."""

    kind = "s3"
    snapshot = True
    requires_credentials = True

    def _bucket(self) -> str:
        return str(self.config.get("bucket") or settings.s3_bucket or "").strip()

    def _prefix(self) -> str:
        return str(self.config.get("prefix") or "").strip()

    def connect(self) -> None:
        try:
            import boto3  # noqa: F401 - availability probe only
        except ImportError as exc:
            raise SourceConfigError(
                "boto3 is not installed; the s3 connector is unavailable"
            ) from exc
        if not self._bucket():
            raise SourceConfigError(
                "S3 bucket is not configured (set s3_bucket or connector bucket)"
            )

    def _client(self):
        import boto3

        kwargs: dict = {}
        if settings.s3_endpoint_url:
            kwargs["endpoint_url"] = settings.s3_endpoint_url
        if settings.s3_region:
            kwargs["region_name"] = settings.s3_region
        if settings.s3_access_key:
            kwargs["aws_access_key_id"] = settings.s3_access_key
        if settings.s3_secret_key:
            kwargs["aws_secret_access_key"] = settings.s3_secret_key
        return boto3.client("s3", **kwargs)

    def _read_text(self, client, key: str) -> str:
        """First MAX_SOURCE_BYTES of a text object; read failure = no content."""
        try:
            body = client.get_object(Bucket=self._bucket(), Key=key)["Body"]
            raw = body.read(MAX_SOURCE_BYTES)
        except Exception:
            return ""  # one unreadable object must not fail the whole page
        return raw.decode("utf-8", errors="replace")

    def _doc(self, client, obj: dict, *, key: str, mime: str) -> SourceDocumentDoc:
        bucket = self._bucket()
        content = self._read_text(client, key) if _is_text(mime) else ""
        return SourceDocumentDoc(
            remote_id=canonical_remote_id(key),
            title=key.rsplit("/", 1)[-1] or key,
            mime_type=mime,
            updated_at=_naive_utc(obj.get("LastModified")),
            checksum=str(obj.get("ETag") or "").strip('"'),
            asset_reference=f"s3://{bucket}/{key}",
            content=content,
            meta={
                "size": obj.get("Size"),
                "storage_class": str(obj.get("StorageClass") or ""),
                "key": key,
            },
        )

    def list(self, *, cursor: str | None = None) -> tuple[list[SourceDocumentDoc], str | None]:
        self.connect()
        client = self._client()
        kwargs: dict = {"Bucket": self._bucket(), "MaxKeys": _PAGE_SIZE}
        prefix = self._prefix()
        if prefix:
            kwargs["Prefix"] = prefix
        if cursor:
            kwargs["ContinuationToken"] = str(cursor)
        try:
            resp = client.list_objects_v2(**kwargs)
        except Exception as exc:
            raise SourceError(f"s3 list failed: {type(exc).__name__}: {exc}") from exc
        docs: list[SourceDocumentDoc] = []
        for obj in resp.get("Contents") or []:
            key = str(obj.get("Key") or "")
            if not key or key.endswith("/"):
                continue  # folder placeholder keys are not documents
            mime = _mime_for(key, obj.get("ContentType"))
            docs.append(self._doc(client, obj, key=key, mime=mime))
        next_cursor = str(resp.get("NextContinuationToken") or "")
        return docs, next_cursor

    def fetch(self, remote_id: str) -> SourceDocumentDoc:
        self.connect()
        client = self._client()
        key = str(remote_id or "")
        try:
            resp = client.get_object(Bucket=self._bucket(), Key=key)
            body = resp.get("Body")
            raw = body.read(MAX_SOURCE_BYTES) if body is not None else b""
        except Exception as exc:
            raise SourceError(
                f"s3 object not available: {key[:120]} ({type(exc).__name__})"
            ) from exc
        mime = _mime_for(key, resp.get("ContentType"))
        content = raw.decode("utf-8", errors="replace") if _is_text(mime) else ""
        return SourceDocumentDoc(
            remote_id=canonical_remote_id(key),
            title=key.rsplit("/", 1)[-1] or key,
            mime_type=mime,
            updated_at=_naive_utc(resp.get("LastModified")),
            checksum=str(resp.get("ETag") or "").strip('"'),
            asset_reference=f"s3://{self._bucket()}/{key}",
            content=content,
            meta={"key": key},
        )


def _naive_utc(value):
    """S3 datetimes are tz-aware; persist naive UTC like every other adapter."""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


__all__ = ["S3Connector"]
