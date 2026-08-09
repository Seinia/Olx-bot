from curl_cffi import requests as cffi_requests
from curl_cffi.curl import CurlError

from olx.errors import BlockedError, TransportError

BLOCKED_STATUS_CODES = {403, 429}


def fetch(url: str, *, timeout: float = 30.0, proxy: str | None = None) -> bytes:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = cffi_requests.get(url, impersonate="chrome", timeout=timeout, proxies=proxies)
    except CurlError as e:  # общий предок Timeout/ConnectionError/DNSError
        raise TransportError(f"{url}: {e}") from e

    _raise_for_status(response.status_code, url)
    return response.content


def _raise_for_status(status_code: int, url: str) -> None:
    if status_code in BLOCKED_STATUS_CODES:
        raise BlockedError(f"{url} вернул {status_code}")
    if status_code != 200:
        raise TransportError(f"{url} вернул {status_code}")
