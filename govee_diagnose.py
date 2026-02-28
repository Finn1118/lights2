#!/usr/bin/env python3
"""
Govee LAN Discovery Diagnostic Tool
=====================================
Runs four sequential tests to pinpoint exactly where UDP multicast fails.
Each test isolates one layer of the stack independently.

SETUP: Set DEVICE_IP below to the IP shown in your router's client list.
"""

import json
import socket
import struct
import time
import threading

# ── Set this to your light's IP from the router client list ─────────
DEVICE_IP = "192.168.8.233"  # e.g. "192.168.8.105"
# ────────────────────────────────────────────────────────────────────

MULTICAST_ADDR = "239.255.255.250"
DISCOVERY_PORT = 4001
CONTROL_PORT   = 4003
SCAN_PAYLOAD   = json.dumps({
    "msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}
}).encode()


# ── Helpers ──────────────────────────────────────────────────────────

def get_lan_ip():
    """Route-based detection of the active LAN interface IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def banner(title):
    print(f"\n{'='*62}")
    print(f"  {title}")
    print('='*62)

def result(tag, msg):
    icons = {"PASS": "✓", "FAIL": "✗", "WARN": "!", "SKIP": "-"}
    print(f"  [{icons.get(tag, '?')} {tag}] {msg}")


# ── Test 1: Windows Firewall / UDP Stack ─────────────────────────────

def test_1_firewall_loopback():
    """
    Sends multicast with IP_MULTICAST_LOOP=1.
    We should receive our own packet on UDP 4001.
    If we don't: Windows Firewall is blocking inbound UDP 4001.
    If we do:    The Windows network stack is functional, firewall is OK.
    """
    banner("TEST 1  ·  Windows Firewall & UDP Stack (Loopback Probe)")
    print("  Sends multicast to itself via loopback. Windows Firewall")
    print("  must allow inbound UDP 4001 for this to succeed.\n")

    received = threading.Event()
    rx_data  = []

    def _listener():
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", DISCOVERY_PORT))
        mreq = struct.pack("4sL", socket.inet_aton(MULTICAST_ADDR), socket.INADDR_ANY)
        rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        rx.settimeout(3.0)
        try:
            data, addr = rx.recvfrom(4096)
            rx_data.append((data, addr))
            received.set()
        except socket.timeout:
            pass
        finally:
            rx.close()

    t = threading.Thread(target=_listener, daemon=True)
    t.start()
    time.sleep(0.15)  # let listener bind before sending

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)  # echo to self
    tx.sendto(SCAN_PAYLOAD, (MULTICAST_ADDR, DISCOVERY_PORT))
    tx.close()

    t.join(timeout=3.5)

    if received.is_set():
        result("PASS", f"Loopback received from {rx_data[0][1]}")
        print("         Windows stack + Firewall are OK for UDP 4001.")
        return True
    else:
        result("FAIL", "No loopback packet received — Windows Firewall is blocking UDP 4001 inbound.")
        print()
        print("  >>> FIX (run as Administrator):")
        print('  >>> netsh advfirewall firewall add rule name="Govee LAN" dir=in '
              'action=allow protocol=UDP localport=4001')
        return False


# ── Test 2: Multicast with Explicit Interface Binding ───────────────

def test_2_multicast_explicit_interface(lan_ip):
    """
    Single-socket design: bind to (lan_ip, 4001) for both send and receive.

    Why single socket matters:
      - The TX source port must be 4001. Govee responds unicast to the
        source IP:port of the scan. If we send from an ephemeral port and
        listen on 4001, the response lands on the wrong port and is missed.
      - IP_MULTICAST_LOOP=0 prevents Windows from echoing our own outgoing
        multicast back to us, which would produce a false PASS (the previous
        bug: response addr was 192.168.8.118, our own LAN IP, not the Govee).
      - IP_MULTICAST_IF pins the packet to the physical Ethernet adapter so
        it doesn't silently exit on the WSL virtual NIC (172.26.x.x), which
        is what govee_mouse_light.py was doing without this setting.
    """
    banner("TEST 2  ·  Multicast With Correct Interface Binding  (IGMP test)")
    print(f"  LAN interface: {lan_ip}")
    print("  Sending on physical Ethernet only. Self-echo suppressed.")
    print("  Waiting 5s for Govee to respond...\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    # Pin to physical LAN adapter — prevents WSL/Wi-Fi misrouting
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(lan_ip))
    # Suppress self-echo to eliminate false positives
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    # Join multicast group so we receive responses sent to 239.255.255.250
    mreq = struct.pack("4s4s",
                       socket.inet_aton(MULTICAST_ADDR),
                       socket.inet_aton(lan_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    # Bind to lan_ip:4001 — source port 4001 means Govee responds to 4001
    sock.bind((lan_ip, DISCOVERY_PORT))
    sock.settimeout(5.0)

    sock.sendto(SCAN_PAYLOAD, (MULTICAST_ADDR, DISCOVERY_PORT))

    try:
        while True:
            data, addr = sock.recvfrom(4096)
            if addr[0] == lan_ip:
                continue  # residual self-echo guard
            resp = json.loads(data.decode())
            msg  = resp.get("msg", {})
            if msg.get("cmd") == "scan" and "device" in msg.get("data", {}):
                result("PASS", f"Govee at {addr[0]} responded via multicast.")
                print(f"         Full response: {data.decode(errors='replace')}")
                return True
    except socket.timeout:
        result("FAIL", "No Govee multicast response — router is likely dropping it.")
        print("         Primary suspect: IGMP snooping on br-lan.")
        print("         See router fix instructions in the summary.")
        return False
    finally:
        sock.close()


# ── Test 3: Unicast Direct to Device IP (Bypasses Multicast Entirely) ─

def test_3_unicast_direct(lan_ip):
    """
    Sends the scan payload as a unicast UDP packet directly to the device IP.
    If this works but Test 2 failed, the router's multicast/IGMP handling
    is confirmed as the culprit — not the device, not Windows.
    """
    banner("TEST 3  ·  Direct Unicast Scan to Device IP (IGMP Bypass)")
    if not DEVICE_IP:
        result("SKIP", "DEVICE_IP not set at top of script.")
        print("         Edit the script and set DEVICE_IP to the IP from your router.")
        return None

    print(f"  Sending scan payload directly to {DEVICE_IP}:{DISCOVERY_PORT}")
    print("  This skips multicast + IGMP entirely.\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((lan_ip, 0))
    sock.settimeout(5.0)

    sock.sendto(SCAN_PAYLOAD, (DEVICE_IP, DISCOVERY_PORT))

    try:
        data, addr = sock.recvfrom(4096)
        result("PASS", f"Govee responded from {addr[0]}:{addr[1]}")
        print(f"         Full response: {data.decode(errors='replace')}")
        print()
        print("  >>> CONFIRMED: The router IGMP snooping/multicast bridge is the problem.")
        print("  >>> The device is alive and responding — just multicast is broken.")
        print("  >>> See the 'FIX' section below for two solutions.")
        return True
    except socket.timeout:
        result("FAIL", "No unicast response from device either.")
        print("         Either LAN Control is not enabled in the Govee app,")
        print("         or the device IP is incorrect / device is offline.")
        return False
    finally:
        sock.close()


# ── Test 4: Control Port 4003 — Send Color Command ───────────────────

def test_4_control_port(lan_ip):
    """
    Sends a real color command to port 4003.
    The light changes color if 4003 is reachable.
    Govee does not ACK color commands, so we test by visual observation.
    Also sends devStatus which some firmware versions do ACK.
    """
    banner("TEST 4  ·  Control Port 4003 Reachability (Color Command)")
    if not DEVICE_IP:
        result("SKIP", "DEVICE_IP not set.")
        return None

    print(f"  Sending a RED color command to {DEVICE_IP}:{CONTROL_PORT}.")
    print("  Watch the light — if it turns red, port 4003 is fully reachable.\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((lan_ip, 0))
    sock.settimeout(2.0)

    # Turn on first
    sock.sendto(json.dumps({
        "msg": {"cmd": "turn", "data": {"value": 1}}
    }).encode(), (DEVICE_IP, CONTROL_PORT))
    time.sleep(0.1)

    # Set red
    sock.sendto(json.dumps({
        "msg": {"cmd": "colorwc", "data": {
            "color": {"r": 255, "g": 0, "b": 0},
            "colorTemInKelvin": 0
        }}
    }).encode(), (DEVICE_IP, CONTROL_PORT))

    time.sleep(0.1)

    # devStatus — some firmware ACKs this
    sock.sendto(json.dumps({
        "msg": {"cmd": "devStatus", "data": {}}
    }).encode(), (DEVICE_IP, CONTROL_PORT))

    try:
        data, addr = sock.recvfrom(4096)
        result("PASS", f"Port 4003 ACK from {addr}: {data.decode(errors='replace')[:80]}")
        return True
    except socket.timeout:
        result("WARN", "No ACK on 4003 (normal — Govee rarely ACKs control commands).")
        print("         >>> Did the light turn RED? Y = port 4003 is working perfectly.")
        print("         >>> N = firewall or routing is also blocking port 4003.")
        return None
    finally:
        sock.close()


# ── Summary & Guidance ───────────────────────────────────────────────

def print_summary(r1, r2, r3, r4):
    banner("DIAGNOSIS SUMMARY")
    states = {True: "PASS", False: "FAIL", None: "SKIP"}
    print(f"  Test 1  Firewall loopback:           {states[r1]}")
    print(f"  Test 2  Multicast w/ interface bind: {states[r2]}")
    print(f"  Test 3  Unicast direct to device:    {states[r3]}")
    print(f"  Test 4  Control port 4003:           {states[r4]}")
    print()

    if not r1:
        print("  CONCLUSION: Windows Firewall is blocking inbound UDP 4001.")
        print("  Run the netsh rule shown in Test 1 as Administrator, then retry.")

    elif r2:
        print("  CONCLUSION: Multicast is fully working.")
        print("  govee_mouse_light.py should now discover your device successfully.")
        print("  (Test 3 FAIL is normal — Govee firmware does not respond to unicast scan.)")

    elif r1 and not r2 and r3:
        # Rare: multicast broken but unicast scan works (non-standard firmware)
        print("  CONCLUSION: Router IGMP snooping is dropping the multicast response.")
        print()
        print("  FIX A — Router SSH (GL.iNet Opal):")
        print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_snooping")
        print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_querier")
        print("    # Persist: add both lines to /etc/rc.local before 'exit 0'")
        print()
        print("  FIX B — Skip discovery, use unicast script:")
        print("    Set DEVICE_IP in govee_mouse_light_unicast.py and run that instead.")

    elif r1 and not r2 and not r3:
        # Most common real-world case: multicast broken, unicast scan unsupported
        print("  CONCLUSION: Multicast response is not reaching this PC.")
        print("  (Test 3 FAIL is expected — Govee devices ignore unicast scan.)")
        print()
        print("  Two possible causes — try FIX A first:")
        print()
        print("  FIX A — Router IGMP snooping (GL.iNet Opal SSH):")
        print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_snooping")
        print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_querier")
        print("    # Persist: add both lines to /etc/rc.local before 'exit 0'")
        print()
        print("  FIX B — Skip discovery entirely (no router change needed):")
        print("    Set DEVICE_IP in govee_mouse_light_unicast.py and run that instead.")
        print("    Control commands to port 4003 are unicast and unaffected by IGMP.")


def main():
    print("Govee LAN API — UDP Multicast Failure Diagnostic")
    print("=" * 62)
    lan_ip = get_lan_ip()
    print(f"  Active LAN IP:  {lan_ip}")
    print(f"  Target device:  {DEVICE_IP or 'NOT SET'}")

    r1 = test_1_firewall_loopback()
    r2 = test_2_multicast_explicit_interface(lan_ip)
    r3 = test_3_unicast_direct(lan_ip)
    r4 = test_4_control_port(lan_ip)

    print_summary(r1, r2, r3, r4)


if __name__ == "__main__":
    main()
