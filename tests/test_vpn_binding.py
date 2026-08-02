"""Finding the real VPN tunnel, and binding aria2 to it.

macOS keeps several utun devices up permanently for iCloud Private Relay and
Handoff. Treating any UP utun as "the VPN" made the check pass with the VPN
switched off, and would have bound aria2 to a tunnel carrying nothing.
"""

import pytest

from trrnt import vpn as vpn_module
from trrnt.main import _NO_TUNNEL, _resolve_bt_interface
from trrnt.vpn import VPNGuard


# ifconfig output shaped like a real Mac with ProtonVPN up: many utun devices,
# only one with an IPv4 address.
IFCONFIG = {
    "utun4": "utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1420\n"
             "\tinet 10.2.0.2 --> 10.2.0.2 netmask 0xffffffff\n",
    "utun6": "utun6: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380\n"
             "\tinet6 fe80::ce81:b1c:bd2c:69e%utun6 prefixlen 64 scopeid 0xf\n",
    "en0": "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
           "\tinet 192.168.1.151 netmask 0xffffff00 broadcast 192.168.1.255\n",
}


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setattr(vpn_module.platform, "system", lambda: "Darwin")
    return VPNGuard({"enabled": True, "interface_prefix": "utun"})


def _routes(guard, monkeypatch, default_iface):
    """Point the default route at an interface and serve fake ifconfig."""
    def fake_run(*argv):
        if argv[:3] == ("/sbin/route", "-n", "get"):
            return f"   route to: default\n  interface: {default_iface}\n"
        if argv[0] == "/sbin/ifconfig":
            return IFCONFIG.get(argv[1], "")
        return ""
    monkeypatch.setattr(guard, "_run", fake_run)


def test_the_tunnel_carrying_traffic_is_chosen(guard, monkeypatch):
    _routes(guard, monkeypatch, "utun4")
    assert guard.find_vpn_interface() == "utun4"


def test_a_system_tunnel_with_no_ipv4_is_rejected(guard, monkeypatch):
    """utun6 is Private Relay — up, but nothing to bind a socket to."""
    _routes(guard, monkeypatch, "utun6")
    assert guard.find_vpn_interface() is None


def test_vpn_off_is_detected(guard, monkeypatch):
    """Default route on the physical NIC means traffic is not tunnelled —
    the case the old check got wrong by finding a leftover utun."""
    _routes(guard, monkeypatch, "en0")
    assert guard.find_vpn_interface() is None


def test_no_default_route_is_not_a_crash(guard, monkeypatch):
    monkeypatch.setattr(guard, "_run", lambda *a: "")
    assert guard.find_vpn_interface() is None


# ── what aria2 gets bound to ──────────────────────────────────────────────────

class Cfg:
    def __init__(self, data): self._d = data
    def get(self, *keys, default=None):
        o = self._d
        for k in keys:
            if isinstance(o, dict):
                o = o.get(k)
                if o is None: return default
            else: return default
        return o


def _detects(monkeypatch, interface):
    monkeypatch.setattr(VPNGuard, "find_vpn_interface", lambda self: interface)


def test_the_detected_tunnel_is_used(monkeypatch):
    _detects(monkeypatch, "utun4")
    cfg = Cfg({"aria2": {"bt_interface": ""}, "vpn": {"enabled": True}})

    assert _resolve_bt_interface(cfg) == "utun4"


def test_startup_is_refused_when_no_tunnel_is_up(monkeypatch):
    """Fail closed: starting unbound would put BitTorrent on the ISP link."""
    _detects(monkeypatch, None)
    cfg = Cfg({"aria2": {"bt_interface": ""}, "vpn": {"enabled": True}})

    assert _resolve_bt_interface(cfg) is _NO_TUNNEL


def test_a_hand_pinned_interface_wins(monkeypatch):
    _detects(monkeypatch, "utun4")
    cfg = Cfg({"aria2": {"bt_interface": "utun9"}, "vpn": {"enabled": True}})

    assert _resolve_bt_interface(cfg) == "utun9"


def test_none_opts_out_explicitly(monkeypatch):
    _detects(monkeypatch, None)
    cfg = Cfg({"aria2": {"bt_interface": "none"}, "vpn": {"enabled": True}})

    assert _resolve_bt_interface(cfg) == ""


def test_vpn_disabled_means_no_binding(monkeypatch):
    _detects(monkeypatch, None)
    cfg = Cfg({"aria2": {"bt_interface": ""}, "vpn": {"enabled": False}})

    assert _resolve_bt_interface(cfg) == ""
