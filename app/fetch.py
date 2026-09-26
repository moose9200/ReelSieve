"""Outbound fetches of user-influenced URLs: public http(s) hosts only, every redirect re-checked,
bodies bounded. Keeps a listing link from reaching the cloud metadata service or private network."""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 25 * 1024 * 1024


def check(url):
    p = urlsplit(url or '')
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise ValueError('Only public http(s) links are supported')
    if p.port not in (None, 80, 443):
        raise ValueError('Only standard web ports are supported')
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ValueError('That address could not be resolved') from None
    if not infos or not all(ipaddress.ip_address(i[4][0].split('%')[0]).is_global for i in infos):
        raise ValueError('That address is not a public website')
    return url


def get(url, headers=None, timeout=45, max_bytes=MAX_BYTES, redirects=5):
    """(final_url, body bytes). Raises ValueError for unsafe targets, oversize bodies or HTTP errors."""
    # ponytail: DNS is checked before each connection; a rebinding resolver could still swap the
    # address in between. Pin the resolved IP in a custom transport if that threat becomes real.
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=False) as client:
        for _ in range(redirects + 1):
            check(url)
            with client.stream('GET', url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get('location', ''))
                    continue
                if r.status_code >= 400:
                    raise ValueError(f'The page answered {r.status_code}')
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise ValueError('That page is too large to read')
                return url, bytes(body)
    raise ValueError('Too many redirects')
