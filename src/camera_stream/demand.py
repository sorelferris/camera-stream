"""Per-camera demand derived from ZeroMQ XPUB subscription events."""

from __future__ import annotations

from collections import Counter


class TopicDemand:
    """Track active subscription prefixes and their matching camera streams.

    The interface deliberately accepts raw XPUB events. Callers do not need to
    understand duplicate subscriptions, empty-prefix subscriptions, or topic
    prefix matching; they only read each camera's current demand count.
    """

    def __init__(self, camera_names: list[str]) -> None:
        self._topics = {name: f"{name}/color".encode() for name in camera_names}
        self._prefix_counts: Counter[bytes] = Counter()
        self._camera_counts: Counter[str] = Counter()

    def apply(self, event: bytes) -> set[str]:
        """Apply one XPUB subscribe/unsubscribe event and return changed cameras."""
        if not event or event[0] not in {0, 1}:
            return set()
        subscribe = event[0] == 1
        prefix = event[1:]
        previous = self._prefix_counts[prefix]
        if subscribe:
            self._prefix_counts[prefix] += 1
            delta = 1
        elif previous:
            self._prefix_counts[prefix] -= 1
            if not self._prefix_counts[prefix]:
                del self._prefix_counts[prefix]
            delta = -1
        else:
            return set()

        changed = set()
        for name, topic in self._topics.items():
            if topic.startswith(prefix):
                self._camera_counts[name] += delta
                changed.add(name)
        return changed

    def count(self, camera_name: str) -> int:
        return self._camera_counts[camera_name]
