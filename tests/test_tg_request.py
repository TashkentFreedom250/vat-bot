import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

from src import tg_request
from src.tg_request import ResilientRequest


def _failure(kind: type, cause: Exception) -> Exception:
    """An error shaped the way HTTPXRequest raises it (`raise X from err`)."""
    try:
        raise kind("boom") from cause
    except kind as exc:
        return exc


class ResilientRequestTests(unittest.TestCase):
    def _run(self, side_effect):
        async def go():
            request = ResilientRequest()
            with patch.object(HTTPXRequest, "do_request", new=AsyncMock(side_effect=side_effect)) as inner, \
                 patch.object(tg_request, "RETRY_DELAYS_S", (0, 0, 0)):
                try:
                    return await request.do_request("https://api.telegram.org/botX/sendMessage", "POST"), inner
                except Exception as exc:  # hand the error back with the call count
                    return exc, inner
        return asyncio.run(go())

    def test_retries_when_the_connection_could_not_be_made(self) -> None:
        result, inner = self._run([
            _failure(TimedOut, httpx.ConnectTimeout("t")),
            _failure(NetworkError, httpx.ConnectError("dns")),
            (200, b"ok"),
        ])
        self.assertEqual(result, (200, b"ok"))
        self.assertEqual(inner.await_count, 3)

    def test_gives_up_after_the_last_retry(self) -> None:
        result, inner = self._run([_failure(TimedOut, httpx.ConnectTimeout("t"))] * 10)
        self.assertIsInstance(result, TimedOut)
        self.assertEqual(inner.await_count, 4)  # 1 try + 3 retries

    def test_does_not_retry_once_the_request_may_have_been_sent(self) -> None:
        # A read timeout means Telegram may already have acted: retrying
        # could post the same message twice.
        for cause in (httpx.ReadTimeout("r"), httpx.ReadError("reset"), httpx.WriteTimeout("w")):
            kind = TimedOut if isinstance(cause, httpx.TimeoutException) else NetworkError
            result, inner = self._run([_failure(kind, cause), (200, b"ok")])
            self.assertIsInstance(result, kind, cause)
            self.assertEqual(inner.await_count, 1, cause)


if __name__ == "__main__":
    unittest.main()
