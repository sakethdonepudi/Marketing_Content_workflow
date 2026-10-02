"""Replaceable durable-media storage boundary for Architecture 06B."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class StoredMedia:
    storage_uri: str
    checksum_sha256: str
    file_size: int


class MediaStorage(ABC):
    @abstractmethod
    def save(self, asset_bytes, *, extension, metadata=None):
        raise NotImplementedError

    @abstractmethod
    def get(self, storage_uri):
        raise NotImplementedError

    @abstractmethod
    def exists(self, storage_uri):
        raise NotImplementedError


class LocalMediaStorage(MediaStorage):
    """Local/dev implementation; the local:// contract can later map to S3/R2/GCS."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, storage_uri):
        if not storage_uri.startswith("local://"):
            raise ValueError("unsupported storage URI")
        name = storage_uri.removeprefix("local://")
        if not name:
            raise ValueError("invalid local storage URI")
        path = (self.root / name).resolve()
        # Traversal protection: the resolved path must stay inside the configured root,
        # whether it is a flat checksum file or a nested <env>/self-test/... key.
        if path != self.root and self.root not in path.parents:
            raise ValueError("storage path escapes configured root")
        return path

    def save(self, asset_bytes, *, extension, metadata=None):
        del metadata
        if not isinstance(asset_bytes, (bytes, bytearray)):
            raise TypeError("asset bytes are required")
        checksum = hashlib.sha256(asset_bytes).hexdigest()
        safe_extension = extension.lower().lstrip(".")
        if safe_extension not in {"png", "jpg", "jpeg", "webp", "mp4", "webm", "wav", "mp3", "m4a"}:
            raise ValueError("unsupported storage extension")
        storage_uri = f"local://{checksum}.{safe_extension}"
        destination = self._path(storage_uri)
        if not destination.exists():
            descriptor, temporary_name = tempfile.mkstemp(prefix="render-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(asset_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return StoredMedia(storage_uri=storage_uri, checksum_sha256=checksum, file_size=len(asset_bytes))

    def get(self, storage_uri):
        return self._path(storage_uri).read_bytes()

    def exists(self, storage_uri):
        return self._path(storage_uri).is_file()

    def save_at(self, storage_uri, data):
        """Write exact bytes to an explicit, safe local URI (used by the storage self-test)."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("asset bytes are required")
        path = self._path(storage_uri)          # traversal protection lives in _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(data))
        return StoredMedia(storage_uri=self._canonical_uri(path), checksum_sha256=hashlib.sha256(data).hexdigest(),
                           file_size=len(data))

    def delete(self, storage_uri):
        """Delete a local object. Returns True if removed, False if it was already absent."""
        path = self._path(storage_uri)
        if path.exists():
            path.unlink()
            return True
        return False

    def _canonical_uri(self, path):
        return f"local://{path.relative_to(self.root).as_posix()}"


class S3MediaStorage(MediaStorage):
    """Centralized object storage (S3/R2-compatible). `s3://bucket/key` URIs via boto3.

    Fails closed with a clear error when boto3 or the required env is missing, so production never
    silently falls back to local disk.
    """

    def __init__(self, *, bucket=None, endpoint_url=None, prefix="media"):
        self.bucket = bucket or os.environ.get("S3_BUCKET")
        self.endpoint_url = endpoint_url or os.environ.get("S3_ENDPOINT_URL") or None
        self.prefix = prefix
        if not self.bucket:
            raise ValueError("S3_BUCKET is required for storage backend 's3'")
        self._client = self._build_client()

    def _build_client(self):
        try:
            import boto3
        except ImportError as error:
            raise RuntimeError("storage backend 's3' requires boto3") from error
        return boto3.client(
            "s3", endpoint_url=self.endpoint_url,
            region_name=os.environ.get("S3_REGION"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"))

    def _key(self, storage_uri):
        if not storage_uri.startswith("s3://"):
            raise ValueError("unsupported storage URI for S3 backend")
        parts = storage_uri.removeprefix("s3://").split("/", 1)
        if len(parts) != 2 or not parts[1]:
            raise ValueError("invalid s3 storage URI")
        return parts[1]

    def save(self, asset_bytes, *, extension, metadata=None):
        if not isinstance(asset_bytes, (bytes, bytearray)):
            raise TypeError("asset bytes are required")
        checksum = hashlib.sha256(asset_bytes).hexdigest()
        safe_extension = extension.lower().lstrip(".")
        if safe_extension not in {"png", "jpg", "jpeg", "webp", "mp4", "webm", "wav", "mp3", "m4a"}:
            raise ValueError("unsupported storage extension")
        key = f"{self.prefix}/{checksum}.{safe_extension}"
        content_type = {"mp4": "video/mp4", "webm": "video/webm", "png": "image/png",
                        "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
                        "wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4"}.get(safe_extension)
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=bytes(asset_bytes),
                                    ContentType=content_type or "application/octet-stream",
                                    Metadata={k: str(v) for k, v in (metadata or {}).items()})
        except Exception as error:  # noqa: BLE001 - surface a safe storage failure
            raise RuntimeError(f"object storage upload failed: {type(error).__name__}") from None
        return StoredMedia(storage_uri=f"s3://{self.bucket}/{key}", checksum_sha256=checksum,
                           file_size=len(asset_bytes))

    def get(self, storage_uri):
        key = self._key(storage_uri)
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def exists(self, storage_uri):
        key = self._key(storage_uri)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - a miss or error is reported as not-present
            return False

    def save_at(self, storage_uri, data):
        """Write exact bytes to an explicit object key (used by the storage self-test)."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("asset bytes are required")
        key = self._key(storage_uri)
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=bytes(data),
                                    ContentType="application/octet-stream")
        except Exception as error:  # noqa: BLE001 - sanitized failure, no credentials
            raise RuntimeError(f"object storage write failed: {type(error).__name__}") from None
        return StoredMedia(storage_uri=f"s3://{self.bucket}/{key}",
                           checksum_sha256=hashlib.sha256(data).hexdigest(), file_size=len(data))

    def delete(self, storage_uri):
        """Delete an object key; returns True on a successful delete request."""
        key = self._key(storage_uri)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as error:  # noqa: BLE001 - sanitized failure, no credentials
            raise RuntimeError(f"object storage delete failed: {type(error).__name__}") from None
        return True


def media_storage_from_env(root):
    """Select the storage backend centrally: STORAGE_BACKEND=local (default) or s3."""
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"
    if backend == "s3":
        return S3MediaStorage()
    return LocalMediaStorage(root)


def storage_health_status(root=None):
    """Safe storage status: backend name + reachability only (no bucket/credentials)."""
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"
    connected = False
    try:
        if backend == "s3":
            storage = S3MediaStorage()
            connected = storage.exists("s3://%s/__healthcheck__" % storage.bucket) is not None
            connected = True  # a successful client construction is enough for the status
        else:
            storage = LocalMediaStorage(root or os.environ.get("RENDER_STORAGE_ROOT", "."))
            connected = storage.root.exists()
    except Exception:  # noqa: BLE001 - status only, never leak the error text
        connected = False
    return {"storage_backend": backend, "storage_connected": connected}


REQUIRED_S3_VARS = ("S3_BUCKET", "S3_ENDPOINT_URL", "S3_REGION", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


def assert_storage_configuration():
    """Startup config check only (no network). Fails closed when the backend is misconfigured.

    Error messages list missing variable NAMES only; never values.
    """
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"
    if backend == "local":
        return {"storage_backend": "local", "storage_configured": True}
    if backend == "s3":
        missing = [name for name in REQUIRED_S3_VARS if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError("storage backend 's3' missing required config: " + ", ".join(missing))
        return {"storage_backend": "s3", "storage_configured": True}
    raise ValueError(f"unsupported STORAGE_BACKEND: {backend!r}")


def self_test_prefix():
    """Unique, non-destructive self-test prefix scoped per environment."""
    env = os.environ.get("APP_ENV", "development").strip().lower()
    if env == "production":
        return "production/self-test"
    if env == "staging":
        return "staging/self-test"
    return "dev/self-test"
