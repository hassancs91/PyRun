"""
One SSRF guard for every user-supplied outbound URL.

Operators type endpoint URLs into console forms — S3 endpoints, notification
webhooks, vector stores, Brain import URLs — and the web or worker process then
makes requests to them. A URL that points back into the deployment's own network
(loopback, RFC-1918, link-local) would let a console user probe or drive services
only the server can reach, so every such URL is checked here, in one place:

* ``PYRUNNER_ALLOW_PRIVATE_NETWORK`` **off (the default)** — loopback, private,
  link-local and reserved addresses are rejected. The hostname is DNS-resolved
  first, so a public name that points at a private IP is caught as well as a
  literal private IP.
* **on** — loopback, private and reserved addresses are allowed, so a Qdrant,
  MinIO, ntfy or Ollama sibling on the compose network (or on localhost during
  development) is reachable. Only the operator can set it (it is an environment
  variable, never a console setting), so the people the guard defends against
  cannot flip it.
* **Always blocked, whatever the flag says** — the link-local / cloud-metadata
  ranges (``169.254.0.0/16``: ``169.254.169.254`` serves instance credentials on
  AWS, GCP and Azure; ``fe80::/10``; AWS IMDS over IPv6 ``fd00:ec2::254``) and the
  unspecified addresses ``0.0.0.0`` / ``::``. Reaching your own MinIO is a
  legitimate operator choice; leaking the host's cloud credentials never is.

``core.services.s3_service.is_safe_endpoint_url`` re-exports the function so the
original import path keeps working.
"""

from django.conf import settings

from core.script_helpers import pyrunner_netguard as _policy
from core.script_helpers.pyrunner_netguard import (  # noqa: F401 - compatibility
    ALWAYS_BLOCKED_NETWORKS, FLAG_HINT, _check_ip, socket,
)


def private_network_allowed() -> bool:
    return bool(getattr(settings, "PYRUNNER_ALLOW_PRIVATE_NETWORK", False))


def is_safe_endpoint_url(
    url: str, *, allow_private: bool | None = None, operator_hint: bool = True,
) -> tuple[bool, str]:
    """``(ok, reason)``. ``operator_hint=False`` leaves the environment-variable
    hint out of the reason, for pages and messages workspace members see (the UI
    shows no internals); superuser-only forms keep it."""
    ok, why = _policy.is_safe_endpoint_url(
        url, allow_private=private_network_allowed() if allow_private is None else allow_private,
    )
    return ok, (why if operator_hint else without_operator_hint(why))


def without_operator_hint(message: str) -> str:
    """The guard's reason without "(set PYRUNNER_ALLOW_PRIVATE_NETWORK=true …)"."""
    return (message or "").replace(FLAG_HINT, "")


# --- Connect-time pinning (F130) -------------------------------------------
# is_safe_endpoint_url resolves the host once to validate it; the HTTP client
# then resolves it again to connect. A DNS-rebinding name can answer a public IP
# to the first lookup and 127.0.0.1 / 169.254.169.254 to the second. The helpers
# below validate at connect time and connect to the address they validated, on
# every hop (each redirect opens a new connection through the same check).


class UnsafeAddressError(OSError):
    """The resolved address is refused by the SSRF policy."""


def guarded_create_connection(
    address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *,
    allow_private=None, socket_options=None,
):
    """``socket.create_connection`` that only connects to policy-approved IPs."""
    host, port = address
    allow = private_network_allowed() if allow_private is None else allow_private
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    for *_, sockaddr in infos:
        ok, message = _check_ip(sockaddr[0], allow)
        if not ok:
            # Connect-time refusals end up in member-visible errors (a connection's
            # Test, a watcher check), so they never name the setting.
            raise UnsafeAddressError(without_operator_hint(message))
    error = None
    for family, socktype, proto, _, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            for option in socket_options or ():
                sock.setsockopt(*option)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error or OSError(f"No address found for {host}")


def _guarded_http_class(cls):
    def factory(host, **kwargs):
        conn = cls(host, **kwargs)
        conn._create_connection = guarded_create_connection
        return conn

    return factory


def guarded_opener(*handlers):
    """A urllib opener whose HTTP(S) connections go through the SSRF check."""
    import http.client
    import urllib.request

    class GuardedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_guarded_http_class(http.client.HTTPConnection), req)

    class GuardedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(
                _guarded_http_class(http.client.HTTPSConnection), req, context=self._context
            )

    return urllib.request.build_opener(GuardedHTTPHandler(), GuardedHTTPSHandler(), *handlers)


def mount_guarded_adapters(session):
    """Route a requests.Session's http/https connections through the SSRF check."""
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError

    def new_conn(self):
        try:
            return guarded_create_connection(
                (self._dns_host, self.port), self.timeout,
                source_address=self.source_address, socket_options=self.socket_options,
            )
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc
        except TimeoutError as exc:
            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out.") from exc
        except OSError as exc:
            raise NewConnectionError(self, f"Failed to establish a new connection: {exc}") from exc

    http_conn = type("GuardedHTTPConnection", (HTTPConnection,), {"_new_conn": new_conn})
    https_conn = type("GuardedHTTPSConnection", (HTTPSConnection,), {"_new_conn": new_conn})
    pools = {
        "http": type("GuardedHTTPPool", (HTTPConnectionPool,), {"ConnectionCls": http_conn}),
        "https": type("GuardedHTTPSPool", (HTTPSConnectionPool,), {"ConnectionCls": https_conn}),
    }

    class GuardedAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = pools

    adapter = GuardedAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
