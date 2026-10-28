"""New offline policy for the recovered source tree.

The shipped app has cloud calls in DeepSeek/Gemini/Google Translate/Edge TTS.
This module allows only loopback traffic, so a missing local model fails clearly
instead of silently using the cloud. Model downloads are intentionally done in a
separate provisioning run before this policy is enabled.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
import re
import urllib.parse
import urllib.request


_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_URLOPEN = urllib.request.urlopen
_INSTALLED = False


class OfflineNetworkError(OSError):
    pass


def is_loopback_host(host) -> bool:
    host = str(host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url))
        return parsed.scheme in ("http", "https", "ws", "wss") and is_loopback_host(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def install_network_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    def audit(event, args):
        if event in ("socket.connect", "socket.bind"):
            sock, address = args[:2]
            if sock.family not in (socket.AF_INET, socket.AF_INET6):
                return
            if not is_loopback_host(address[0]):
                raise OfflineNetworkError("offline mode: only loopback sockets are allowed")
        elif event == "socket.getaddrinfo":
            if args[0] is not None and not is_loopback_host(args[0]):
                raise OfflineNetworkError("offline mode: external DNS lookups are disabled")
        elif event == "subprocess.Popen":
            argv = args[1]
            for arg in argv if isinstance(argv, (tuple, list)) else (str(argv),):
                for match in re.finditer(r"(?:https?|wss?|rtsp|rtmp|ftp|tcp|udp)://[^\s\"']+", str(arg)):
                    if not is_loopback_url(match.group(0)):
                        raise OfflineNetworkError("offline mode: remote subprocess input is disabled")

    sys.addaudithook(audit)

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) and address else ""
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = host.lower() == "localhost"
        if not allowed:
            raise OSError("offline mode: outbound network is disabled")
        return _ORIGINAL_CONNECT(sock, address)

    def create_connection(address, timeout=None, source_address=None):
        host = address[0] if isinstance(address, tuple) and address else ""
        if not is_loopback_host(host):
            raise OSError("offline mode: outbound network is disabled")
        return _ORIGINAL_CREATE_CONNECTION(address, timeout, source_address)

    def urlopen(url, *args, **kwargs):
        target = url if isinstance(url, str) else getattr(url, "full_url", "")
        if not is_loopback_url(target):
            raise OSError("offline mode: outbound network is disabled")
        return _ORIGINAL_URLOPEN(url, *args, **kwargs)

    socket.socket.connect = connect
    socket.create_connection = create_connection
    urllib.request.urlopen = urlopen
    _INSTALLED = True
