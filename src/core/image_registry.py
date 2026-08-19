from threading import Lock
from typing import Any


class ImageRegistry:
    """In-memory map between long image URLs and compact IDs."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._url_to_id: dict[str, str] = {}
        self._id_to_url: dict[str, str] = {}
        self._counter = 0

    def register(self, url: str) -> str:
        if not url or not isinstance(url, str):
            return url

        with self._lock:
            existing_id = self._url_to_id.get(url)
            if existing_id:
                return existing_id

            self._counter += 1
            image_id = f"img_{self._counter}"
            self._url_to_id[url] = image_id
            self._id_to_url[image_id] = url
            return image_id

    def register_many(self, urls: list[str]) -> list[str]:
        return [self.register(url) for url in urls]

    def resolve(self, value: str) -> str:
        if not isinstance(value, str):
            return value
        return self._id_to_url.get(value, value)

    def resolve_many(self, values: list[str]) -> list[str]:
        return [self.resolve(value) for value in values]

    def resolve_nested(self, data: Any) -> Any:
        if isinstance(data, dict):
            return {key: self.resolve_nested(value) for key, value in data.items()}
        if isinstance(data, list):
            return [self.resolve_nested(value) for value in data]
        if isinstance(data, str):
            return self.resolve(data)
        return data


image_registry = ImageRegistry()
