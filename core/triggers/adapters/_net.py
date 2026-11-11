"""HTTP GETs for the built-in adapters: SSRF-guarded, size-capped, time-limited."""

import requests

from core.netguard import is_safe_endpoint_url, mount_guarded_adapters

from .base import MAX_RESPONSE_BYTES, AdapterError

USER_AGENT = "PyRun-Watcher/1.0"
# Redirects followed (each hop goes through the SSRF check again).
MAX_REDIRECTS = 5


def _session() -> requests.Session:
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    mount_guarded_adapters(session)
    return session


def check_url(url) -> str:
    """``url`` if it is an http(s) URL the SSRF policy allows; else AdapterError."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise AdapterError("The URL must start with http:// or https://.")
    ok, message = is_safe_endpoint_url(url)
    if not ok:
        raise AdapterError(message)
    return url


def get(url, *, params=None, accept=None, timeout=20) -> tuple[bytes, str]:
    """GET ``url``; returns ``(body, content_type)``. AdapterError on any failure.

    Error text names the status or the kind of failure, never the URL's
    query (which may carry a key).
    """
    check_url(url)
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    try:
        with _session() as session:
            response = session.get(url, params=params or None, headers=headers, timeout=timeout, stream=True)
            with response:
                if response.status_code >= 400:
                    raise AdapterError(f"The source answered HTTP {response.status_code}.")
                body = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise AdapterError(
                            f"The response is larger than {MAX_RESPONSE_BYTES // (1024 * 1024)} MB."
                        )
                return bytes(body), response.headers.get("Content-Type", "")
    except AdapterError:
        raise
    except requests.Timeout:
        raise AdapterError(f"The source didn't answer within {timeout:g} seconds.") from None
    except requests.TooManyRedirects:
        raise AdapterError(f"The source redirected more than {MAX_REDIRECTS} times.") from None
    except requests.RequestException as e:
        # The guard's refusal arrives wrapped in a connection error.
        reason = e.__class__.__name__
        cause = e
        while cause is not None:
            if cause.__class__.__name__ == "UnsafeAddressError":
                raise AdapterError(str(cause)) from None
            cause = cause.__cause__ or cause.__context__
        raise AdapterError(f"The source could not be reached ({reason}).") from None
