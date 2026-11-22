"""Local QR rendering and short-lived ownership for WeWe login sessions."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import qrcode
from qrcode.image.svg import SvgPathImage


def login_qr_svg(scan_url: str) -> str:
    """Render the opaque upstream login URL without a third-party QR service."""
    image = qrcode.make(scan_url, image_factory=SvgPathImage, box_size=12, border=2)
    svg = image.to_string().decode("utf-8")
    return svg.replace(
        "<svg ",
        '<svg role="img" aria-label="微信扫码登录二维码" ',
        1,
    )


class LoginSessionRegistry:
    """Bind an upstream QR uuid to one doc user; never stores login tokens."""

    def __init__(self, ttl_seconds: int = 130, clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.clock = clock
        self._items: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = [key for key, (_owner, deadline) in self._items.items() if deadline <= now]
        for key in expired:
            self._items.pop(key, None)

    def put(self, login_id: str, user_id: int) -> int:
        if not login_id or len(login_id) > 256:
            raise ValueError("invalid login session id")
        now = self.clock()
        with self._lock:
            self._prune(now)
            # A newly opened modal supersedes older QR codes for the same user.
            for key, (owner, _deadline) in list(self._items.items()):
                if owner == int(user_id):
                    self._items.pop(key, None)
            self._items[login_id] = (int(user_id), now + self.ttl_seconds)
        return self.ttl_seconds

    def check(self, login_id: str, user_id: int) -> None:
        now = self.clock()
        with self._lock:
            self._prune(now)
            item = self._items.get(login_id)
            if item is None or item[0] != int(user_id):
                raise KeyError(login_id)

    def pop(self, login_id: str, user_id: int) -> None:
        self.check(login_id, user_id)
        with self._lock:
            self._items.pop(login_id, None)
