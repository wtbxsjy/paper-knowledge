"""Single HTTP seam for every outbound call in paperkb_core.

All modules call ``http.urlopen`` (never ``urllib.request.urlopen`` directly),
so tests can swap this one attribute for a replay transport.  The User-Agent
carries a contact address only when the user configured one explicitly
(credentials file ``[contact] email`` or ``PAPERKB_CONTACT_EMAIL``) — a
placeholder address is worse than none, and git config is deliberately not
read (that would send the user's address to third parties without consent).
"""
from __future__ import annotations

import urllib.error
import urllib.request

TOOL_NAME = "paper-knowledge"
TOOL_VERSION = "0.1"

urlopen = urllib.request.urlopen     # replaced by tests


def contact_email() -> str:
    from . import credentials
    return credentials.contact_email()


def user_agent() -> str:
    email = contact_email()
    base = f"{TOOL_NAME}/{TOOL_VERSION}"
    return f"{base} (mailto:{email})" if email else base



class _NoImplicitContentType(urllib.request.BaseHandler):
    """Drop the Content-Type urllib adds on its own to requests with a body.

    Runs after AbstractHTTPHandler.do_request_ (handler_order 600 > 500). A
    Content-Type the caller set explicitly is kept.
    """
    handler_order = 600

    def _strip(self, req):
        if not req.has_header("Content-type") or "Content-type" not in req.headers:
            req.unredirected_hdrs.pop("Content-type", None)
        return req

    http_request = https_request = _strip


_put_opener = urllib.request.build_opener(_NoImplicitContentType())   # keeps ProxyHandler (env proxies)


def put_raw(url: str, data: bytes, content_type: str | None = None, timeout: int = 180) -> tuple[int, bytes]:
    """PUT bytes without any implicit Content-Type; honours HTTP(S)_PROXY.

    Plain urllib adds ``Content-Type: application/x-www-form-urlencoded`` to any
    body, which breaks presigned object-storage URLs signed with an empty
    Content-Type (Alibaba OSS: 403 SignatureDoesNotMatch). Replaced by tests.
    """
    req = urllib.request.Request(url, data=data, method="PUT")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with _put_opener.open(req, timeout=timeout) as r:
            return r.status, r.read(1000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(1000)
