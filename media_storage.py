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
        if not name or Path(name).name != name:
            raise ValueError("invalid local storage URI")
        path = (self.root / name).resolve()
        if path.parent != self.root:
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
