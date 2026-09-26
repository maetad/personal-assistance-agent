"""device-watch: watches devices on the LAN by IP and notifies via Telegram when one
connects or disconnects. Presence is a plain ICMP ping against each watched IP - no
router API, no ARP/nmap dependency (the ping already works from inside this container
because outbound LAN traffic is routed/NATed through the host).

ponytail: presence is tracked by IP, not MAC, so a DHCP lease reassigning the watched IP
to a different device would misreport - use a DHCP reservation on the router for the
watched device's IP to avoid this. Upgrade to MAC-based ARP matching (needs host network
mode) if that ever bites.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

logger = logging.getLogger("plugins.device-watch")

POLL_INTERVAL_SECONDS = 60
PING_TIMEOUT_SECONDS = 2
SWEEP_PING_TIMEOUT_SECONDS = 1
SWEEP_MAX_WORKERS = 32

_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_ctx = None


def _watches_path() -> Path:
    from hermes_constants import get_hermes_home  # lazy: keeps module host-importable/testable

    directory = get_hermes_home() / "device-watch"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "watches.json"


def _load_watches() -> dict:
    path = _watches_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        logger.exception("device-watch: corrupt watches.json, starting fresh")
        return {}


def _save_watches(watches: dict) -> None:
    _watches_path().write_text(json.dumps(watches, indent=2))


def _valid_ipv4(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip)
        return True
    except ValueError:
        return False


def _check_online(ip: str, timeout: int = PING_TIMEOUT_SECONDS) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        logger.exception("device-watch: ping failed for %s", ip)
        return False


def _resolve_hostname(ip: str, timeout: float = 1.0) -> Optional[str]:
    """Reverse DNS via the router's local DNS proxy, which resolves LAN clients' DHCP
    hostnames on most consumer routers (including TP-Link) - same source the Tether app uses."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        hostname = socket.gethostbyaddr(ip)[0]
        return hostname.split(".")[0] or None
    except Exception:
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


def _router_base_url(ctx, subnet: Optional[str]) -> Optional[str]:
    """Operator-configured router URL (``plugins.entries.device-watch.settings.router_host``)
    wins; otherwise guessed as the subnet's first host (the usual home-router address)."""
    configured = ctx.get_config("router_host", None) if ctx is not None else None
    if configured:
        return configured
    if not subnet:
        return None
    network = ipaddress.ip_network(subnet, strict=False)
    return f"http://{network.network_address + 1}"


def _get_router_client(base_url: str, password: str, username: str):
    from tplinkrouterc6u import TplinkRouterProvider  # lazy: optional dep, only needed here

    return TplinkRouterProvider.get_client(base_url, password, username)


def _fetch_router_hostnames(ctx, subnet: Optional[str]) -> dict:
    """Best-effort {ip: hostname} straight from the router's own client list - the same
    encrypted local API the TP-Link Tether app uses, via the tplinkrouterc6u library. Empty
    dict when no router_password is configured, or on any failure (never raises)."""
    password = ctx.get_config("router_password", None) if ctx is not None else None
    if not password:
        return {}
    username = ctx.get_config("router_username", "admin") if ctx is not None else "admin"
    base_url = _router_base_url(ctx, subnet)
    if not base_url:
        return {}
    try:
        router = _get_router_client(base_url, password, username)
    except Exception:
        logger.exception("device-watch: router API client init failed")
        return {}
    try:
        # ponytail: not every client class (e.g. TplinkRouterSG, used by newer Archer
        # BE-series routers) implements the context-manager protocol, so authorize/logout
        # explicitly rather than `with router:`. The router only allows one logged-in
        # session, so logout must run even if get_status() fails.
        router.authorize()
        status = router.get_status()
        return {str(device.ipaddr): device.hostname for device in status.devices if device.hostname}
    except Exception:
        logger.exception("device-watch: router API hostname fetch failed")
        return {}
    finally:
        try:
            router.logout()
        except Exception:
            logger.exception("device-watch: router logout failed")


def _guess_subnet(watches: dict) -> Optional[str]:
    """A /24 derived from the first watched device's IP - used when no subnet is configured."""
    for watch in watches.values():
        ip = watch.get("ip")
        if ip and _valid_ipv4(ip):
            return ".".join(ip.split(".")[:3]) + ".0/24"
    return None


def _resolve_subnet(ctx) -> Optional[str]:
    """Operator-configured subnet (``plugins.entries.device-watch.settings.subnet``) wins;
    otherwise guessed from a watched device's IP."""
    configured = ctx.get_config("subnet", None) if ctx is not None else None
    if configured:
        return configured
    return _guess_subnet(_load_watches())


def _sweep_subnet(subnet: str) -> list:
    """Ping every host address in ``subnet`` (CIDR) in parallel; returns responding IPs, sorted."""
    network = ipaddress.ip_network(subnet, strict=False)
    with ThreadPoolExecutor(max_workers=SWEEP_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_check_online, str(host), SWEEP_PING_TIMEOUT_SECONDS): str(host)
            for host in network.hosts()
        }
        online_ips = [futures[future] for future in as_completed(futures) if future.result()]
    return sorted(online_ips, key=lambda ip: tuple(int(part) for part in ip.split(".")))


def _update_state(name: str, watch: dict, online: bool, now: Optional[float] = None) -> Optional[str]:
    """Mutates ``watch`` in place (``online`` + ``changed_at``); returns a notify message
    only on a real known->known transition (a first-ever check just sets the baseline, no
    notification). A reconnect is suppressed - state still updates, just silently - if the
    device was offline for less than ``watch['min_offline_minutes']`` (default 0 = always
    notify), so a brief flap doesn't page you but a real outage still does. Disconnects are
    never suppressed."""
    if now is None:
        now = time.time()
    previous = watch.get("online")
    previous_changed_at = watch.get("changed_at")
    watch["online"] = online
    watch["changed_at"] = now
    if previous is None or previous == online:
        return None
    if online:
        min_offline_minutes = watch.get("min_offline_minutes", 0)
        offline_seconds = now - previous_changed_at if previous_changed_at is not None else None
        if min_offline_minutes and (offline_seconds is None or offline_seconds < min_offline_minutes * 60):
            return None
        return f"\U0001f4f6 {name} just connected to the network."
    return f"\U0001f4f6 {name} just disconnected from the network."


def _notify(message: str) -> None:
    try:
        from tools.send_message_tool import send_message_tool

        send_message_tool({"action": "send", "target": "telegram", "message": message})
    except Exception:
        logger.exception("device-watch: notify failed for message: %s", message)


def _poll_once() -> None:
    watches = _load_watches()
    if not watches:
        return
    changed = False
    for name, watch in watches.items():
        online = _check_online(watch["ip"])
        message = _update_state(name, watch, online)
        if message:
            changed = True
            _notify(message)
    if changed:
        _save_watches(watches)


def _worker_loop() -> None:
    while True:
        try:
            _poll_once()
        except Exception:
            logger.exception("device-watch: poll failed")
        time.sleep(POLL_INTERVAL_SECONDS)


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="device-watch", daemon=True)
            _worker.start()


WATCH_DEVICE_SCHEMA = {
    "name": "watch_device",
    "description": (
        "Start watching a device on the local network by its IPv4 address, and get notified "
        "via Telegram when it connects or disconnects. Use a static/reserved IP (set a DHCP "
        "reservation on the router for this device) so the address doesn't drift to another device."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short label for the device, e.g. 'moms-phone'."},
            "ip": {"type": "string", "description": "The device's IPv4 address on the LAN, e.g. '192.168.0.42'."},
            "min_offline_minutes": {
                "type": "integer",
                "description": (
                    "Suppress the reconnect notification unless the device was offline for at "
                    "least this many minutes (e.g. 60 to ignore brief flaps). Disconnect "
                    "notifications are never suppressed. Default 0 = always notify."
                ),
            },
        },
        "required": ["name", "ip"],
        "additionalProperties": False,
    },
}

UNWATCH_DEVICE_SCHEMA = {
    "name": "unwatch_device",
    "description": "Stop watching a device previously registered with watch_device.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The device's label, as given to watch_device."}},
        "required": ["name"],
        "additionalProperties": False,
    },
}

LIST_WATCHED_DEVICES_SCHEMA = {
    "name": "list_watched_devices",
    "description": "List currently watched devices and their last known online/offline state.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

LIST_CONNECTED_DEVICES_SCHEMA = {
    "name": "list_connected_devices",
    "description": (
        "Ping-sweep the LAN subnet for devices currently online, and flag which ones are on the "
        "watch list (with their watched name). Includes a best-effort hostname: read straight from "
        "the router's client list when plugins.entries.device-watch.settings.router_password is set "
        "(also router_username, default 'admin', and router_host, default guessed as the subnet's "
        "first address), else via reverse DNS, else null. Also includes any watched device that's "
        "currently offline. Takes a few seconds. Needs a subnet - configured via "
        "plugins.entries.device-watch.settings.subnet (e.g. '192.168.0.0/24'), or guessed from an "
        "already-watched device's IP if none is set."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _handle_watch_device(args: dict, **_kw) -> str:
    from tools.registry import tool_result

    name = str(args.get("name") or "").strip()
    ip = str(args.get("ip") or "").strip()
    if not name or not _valid_ipv4(ip):
        return tool_result({"success": False, "error": "name and a valid IPv4 ip are required"})
    try:
        min_offline_minutes = int(args.get("min_offline_minutes") or 0)
    except (TypeError, ValueError):
        return tool_result({"success": False, "error": "min_offline_minutes must be an integer"})
    if min_offline_minutes < 0:
        return tool_result({"success": False, "error": "min_offline_minutes must be >= 0"})

    watches = _load_watches()
    watches[name] = {"ip": ip, "online": None, "changed_at": None, "min_offline_minutes": min_offline_minutes}
    _save_watches(watches)
    _ensure_worker()
    return tool_result({"success": True, "name": name, "ip": ip, "min_offline_minutes": min_offline_minutes})


def _handle_unwatch_device(args: dict, **_kw) -> str:
    from tools.registry import tool_result

    name = str(args.get("name") or "").strip()
    watches = _load_watches()
    if name not in watches:
        return tool_result({"success": False, "error": f"not watching '{name}'"})
    del watches[name]
    _save_watches(watches)
    return tool_result({"success": True, "name": name})


def _handle_list_watched_devices(args: dict, **_kw) -> str:
    from tools.registry import tool_result

    watches = _load_watches()
    devices = [{"name": name, **watch} for name, watch in watches.items()]
    return tool_result({"success": True, "devices": devices})


def _handle_list_connected_devices(args: dict, **_kw) -> str:
    from tools.registry import tool_result

    subnet = _resolve_subnet(_ctx)
    if not subnet:
        return tool_result({
            "success": False,
            "error": (
                "No subnet configured and no watched device to guess one from. Set "
                "plugins.entries.device-watch.settings.subnet (e.g. '192.168.0.0/24') or "
                "watch_device first."
            ),
        })
    try:
        online_ips = _sweep_subnet(subnet)
    except ValueError as exc:
        return tool_result({"success": False, "error": f"invalid subnet {subnet!r}: {exc}"})

    router_hostnames = _fetch_router_hostnames(_ctx, subnet)
    watches = _load_watches()
    ip_to_name = {watch["ip"]: name for name, watch in watches.items()}
    devices = [
        {
            "ip": ip, "online": True, "watched": ip in ip_to_name, "name": ip_to_name.get(ip),
            "hostname": router_hostnames.get(ip) or _resolve_hostname(ip),
        }
        for ip in online_ips
    ]
    seen_ips = set(online_ips)
    devices.extend(
        {"ip": watch["ip"], "online": False, "watched": True, "name": name, "hostname": router_hostnames.get(watch["ip"])}
        for name, watch in watches.items() if watch["ip"] not in seen_ips
    )
    return tool_result({"success": True, "subnet": subnet, "devices": devices})


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_tool(
        name="watch_device", toolset="device-watch", schema=WATCH_DEVICE_SCHEMA,
        handler=_handle_watch_device, description=WATCH_DEVICE_SCHEMA["description"], emoji="\U0001f4e1",
    )
    ctx.register_tool(
        name="unwatch_device", toolset="device-watch", schema=UNWATCH_DEVICE_SCHEMA,
        handler=_handle_unwatch_device, description=UNWATCH_DEVICE_SCHEMA["description"], emoji="\U0001f6ab",
    )
    ctx.register_tool(
        name="list_watched_devices", toolset="device-watch", schema=LIST_WATCHED_DEVICES_SCHEMA,
        handler=_handle_list_watched_devices, description=LIST_WATCHED_DEVICES_SCHEMA["description"], emoji="\U0001f4cb",
    )
    ctx.register_tool(
        name="list_connected_devices", toolset="device-watch", schema=LIST_CONNECTED_DEVICES_SCHEMA,
        handler=_handle_list_connected_devices, description=LIST_CONNECTED_DEVICES_SCHEMA["description"], emoji="\U0001f4e1",
    )
    if _load_watches():
        _ensure_worker()
