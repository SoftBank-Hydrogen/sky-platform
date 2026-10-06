"""Bounded Responses API transport shared by planning and deployment stages."""
from __future__ import annotations

import json
import math
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RETRY_WAIT = 8


class OpenAIHTTPFailure(Exception):
    def __init__(self, status: int, temporary: bool):
        self.status = status
        self.temporary = temporary
        super().__init__(f"HTTP {status}")


def _retry_after_seconds(headers):
    value = headers.get('Retry-After') if headers else None
    if value is None:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is not None:
            return max(0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError, IndexError):
        pass
    return None


def _temporary_error(status, raw):
    if status == 500:
        return True
    if status not in {429, 503}:
        return False
    try:
        error = json.loads(raw).get('error', {})
        code = error.get('code') if isinstance(error, dict) else None
    except (ValueError, AttributeError, TypeError):
        return False
    return (status == 429 and code in {'rate_limit_exceeded', 'slow_down'}
            or status == 503 and code in {'server_is_overloaded', 'slow_down'})


def read_response(request: urllib.request.Request) -> bytes:
    """Retry only a classified HTTP rejection, before any response is consumed."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(2):
        try:
            with opener.open(request, timeout=60) as response:
                return response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            try:
                error_body = exc.read(4097)
            finally:
                exc.close()
            temporary = _temporary_error(status, error_body)
            if attempt == 0 and temporary:
                minimum = _retry_after_seconds(headers)
                delay = (1 if minimum is None else minimum) + random.uniform(0, 0.25)
                if delay <= MAX_RETRY_WAIT:
                    time.sleep(delay)
                    continue
            raise OpenAIHTTPFailure(status, temporary) from None
    raise AssertionError('unreachable')
