"""
A Telegram request class that does not give up on the first failed
connection.

Why this exists: this Mac regularly loses the ability to open NEW
connections for a minute or so (thousands of such errors in the logs;
on 2026-11-12 one hit seconds after a deploy). The bot's long-poll
connection usually survives, so it keeps RECEIVING messages — but each
reply needs a connection of its own, and python-telegram-bot makes
exactly one attempt. The reply was simply lost after a 30-second wait,
and to the user the bot looked dead.

Only one kind of failure is retried: the connection could not be
established at all (DNS failure, connect error, connect timeout). In
that case nothing was sent, so trying again cannot produce a duplicate
message. A request that connected and then failed (read timeout, reset
mid-reply) is NOT retried here — Telegram may already have acted on it.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

logger = logging.getLogger("vat_bot.telegram")

# Pause before each retry. With a 10 s connect timeout this keeps
# trying for roughly a minute and a half — a late answer beats none.
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 3.0, 6.0, 10.0, 15.0)


def never_reached_telegram(exc: BaseException) -> bool:
    """True when the request failed before any connection existed."""
    return isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout))


class ResilientRequest(HTTPXRequest):
    """HTTPXRequest that retries when the connection could not be made."""

    async def do_request(self, url, method, *args, **kwargs):
        attempt = 0
        while True:
            try:
                return await super().do_request(url, method, *args, **kwargs)
            except (NetworkError, TimedOut) as exc:
                if attempt >= len(RETRY_DELAYS_S) or not never_reached_telegram(exc):
                    raise
                delay = RETRY_DELAYS_S[attempt]
                attempt += 1
                logger.warning(
                    "Could not connect to Telegram for %s (%s) — retry %s of %s in %.0fs",
                    url.rsplit("/", 1)[-1],
                    type(exc.__cause__).__name__,
                    attempt, len(RETRY_DELAYS_S), delay,
                )
                await asyncio.sleep(delay)
