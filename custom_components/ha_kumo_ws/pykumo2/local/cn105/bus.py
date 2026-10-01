"""Send and receive raw CN105 frames through a Kumo adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ...clock import Clock, SystemClock
from ...errors import LocalError
from ..client import LocalUnitClient
from . import (
    DEFAULT_INFO_CODES,
    INFO_RESPONSE_TYPE,
    MAX_FRAME_LEN,
    build_info_request,
    valid_cn105_reply,
)

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.5
REPLY_TIMEOUT_SECONDS = 20.0

_READ_BODY = b'{"c":{"indoorUnit":{"settings":{"rawITPFrame":{}}}}}'
_SYSTEM_CLOCK = SystemClock()


def _frame_body(frame: bytes, id_byte: int) -> bytes:
    payload = {
        "c": {
            "indoorUnit": {
                "settings": {
                    "rawITPFrame": {"frame": frame.hex(), "len": len(frame), "id": id_byte}
                }
            }
        }
    }
    return json.dumps(payload, separators=(",", ":")).encode()


class AsyncCn105Bus:
    """Talks to one adapter's ``rawITPFrame`` node."""

    def __init__(
        self,
        client: LocalUnitClient,
        name: str,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Clock = _SYSTEM_CLOCK,
    ) -> None:
        self._client = client
        self._name = name
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._answered: set[int] = set()
        self._unsupported: set[int] = set()

    @property
    def unsupported_codes(self) -> frozenset[int]:
        """Codes that never answered. :meth:`read_info` will not send them."""
        return frozenset(self._unsupported)

    def forget_unsupported(self) -> None:
        """Allow dropped codes to be sent again, for example after a reboot."""
        self._unsupported.clear()

    async def send(self, frame: bytes, id_byte: int = 1) -> bool:
        """Send a raw frame. True if the adapter accepted it."""
        async with self._lock:
            return await self._send(frame, id_byte)

    async def read(self) -> bytes | None:
        """Read the reply the adapter is holding, or None."""
        async with self._lock:
            return await self._read()

    async def transceive(
        self,
        frame: bytes,
        id_byte: int = 1,
        expect_type: int | None = None,
        expect_code: int | None = None,
        timeout: float = REPLY_TIMEOUT_SECONDS,  # noqa: ASYNC109
    ) -> bytes | None:
        """Send one frame and poll until a matching reply arrives."""
        async with self._lock, self._client.cycle():
            if not await self._send(frame, id_byte):
                return None
            polls = max(1, int(timeout / POLL_INTERVAL_SECONDS))
            for _ in range(polls):
                await self._sleep(POLL_INTERVAL_SECONDS)
                reply = await self._read()
                if reply is None or not valid_cn105_reply(reply):
                    continue
                if expect_type is not None and reply[1] != expect_type:
                    continue
                if expect_code is not None and reply[5] != expect_code:
                    continue
                return reply
        _LOGGER.debug("%s: no valid CN105 reply within %.1fs", self._name, timeout)
        return None

    async def read_info(
        self,
        code: int,
        timeout: float = REPLY_TIMEOUT_SECONDS,  # noqa: ASYNC109
    ) -> bytes | None:
        """Ask for info ``code`` and return the reply, or None."""
        if code in self._unsupported:
            return None
        reply = await self.transceive(
            build_info_request(code),
            expect_type=INFO_RESPONSE_TYPE,
            expect_code=code,
            timeout=timeout,
        )
        if reply is not None:
            self._answered.add(int(code))
            return reply
        if code not in DEFAULT_INFO_CODES and code not in self._answered:
            self._unsupported.add(int(code))
            _LOGGER.warning(
                "%s: info code 0x%02x did not answer and will not be sent again",
                self._name,
                code,
            )
        return None

    async def _send(self, frame: bytes, id_byte: int) -> bool:
        frame = bytes(frame)
        length = len(frame)
        if not 1 <= length <= MAX_FRAME_LEN:
            _LOGGER.warning(
                "%s: raw CN105 frame length %d out of range 1..%d",
                self._name,
                length,
                MAX_FRAME_LEN,
            )
            return False
        if not 0 <= id_byte <= 0xFF:
            _LOGGER.warning("%s: CN105 id byte out of range", self._name)
            return False
        response = await self._exchange(_frame_body(frame, id_byte))
        if not isinstance(response, dict) or not response or "_api_error" in response:
            _LOGGER.warning("%s: failed to send raw CN105 frame", self._name)
            return False
        return True

    async def _read(self) -> bytes | None:
        response = await self._exchange(_READ_BODY)
        if not isinstance(response, dict):
            return None
        try:
            node = response["r"]["indoorUnit"]["settings"]["rawITPFrame"]
        except (KeyError, TypeError):
            return None
        hexstr = node.get("frame") if isinstance(node, dict) else None
        if not isinstance(hexstr, str) or not hexstr:
            return None
        try:
            return bytes.fromhex(hexstr)
        except ValueError:
            _LOGGER.warning("%s: raw CN105 readback is not valid hex", self._name)
            return None

    async def _exchange(self, body: bytes) -> Any:
        try:
            return await self._client.request(body)
        except LocalError:
            return None
