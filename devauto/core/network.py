from __future__ import annotations

import ipaddress
import socket

from devauto.core.config import Settings


WILDCARD_BIND_HOSTS = {"0.0.0.0", "::"}


def qa_access_host(settings: Settings) -> str:
    return browser_host_for_bind(settings.preview_host)


def browser_host_for_bind(host: str) -> str:
    if host in WILDCARD_BIND_HOSTS:
        return "localhost"
    return host


def lan_candidate_hosts(settings: Settings) -> list[str]:
    if settings.bind_host not in WILDCARD_BIND_HOSTS:
        return []
    hosts: list[str] = []
    seen: set[str] = set()
    for address in local_interface_addresses():
        if address not in seen:
            hosts.append(address)
            seen.add(address)
    return hosts


def local_interface_addresses() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        return []
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        host = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            continue
        addresses.append(str(address))
    return addresses


def format_url_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6:
        return f"[{address}]"
    return str(address)


def lan_access_status(settings: Settings) -> str:
    if settings.bind_host not in WILDCARD_BIND_HOSTS:
        return "local-only" if qa_access_host(settings) in {"127.0.0.1", "localhost", "::1"} else "host-restricted"
    if qa_access_host(settings) in {"127.0.0.1", "localhost", "::1"}:
        return "lan-host-needed"
    return "lan-ready"


def preview_url_for_settings(settings: Settings, port: int) -> str:
    return f"http://{format_url_host(qa_access_host(settings))}:{port}"


def preview_lan_urls_for_settings(settings: Settings, port: int) -> list[str]:
    return [f"http://{format_url_host(host)}:{port}" for host in lan_candidate_hosts(settings)]
