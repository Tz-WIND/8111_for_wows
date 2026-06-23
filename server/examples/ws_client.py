#!/usr/bin/env python3
"""
Example consumer for the 8111-for-WoWS server.

Demonstrates both transports using ONLY the standard library:
  * a minimal WebSocket client that streams the live `/all` snapshot, and
  * a plain REST poll of `/map_obj.json`.

Usage:
  python ws_client.py                          # stream 5 WS snapshots
  python ws_client.py --messages 20
  python ws_client.py --host 127.0.0.1 --port 8111
  python ws_client.py --rest                   # one REST poll instead of WS
"""
import argparse
import base64
import json
import os
import socket
import struct
import sys
import urllib.request

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_connect(host, port, path="/ws", timeout=5.0):
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ) % (path, host, port, key)
    sock.sendall(req.encode("ascii"))

    # read response headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1024)
        if not chunk:
            raise RuntimeError("server closed during handshake")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0].decode("latin-1")
    if "101" not in status:
        raise RuntimeError("handshake failed: %s" % status)
    return sock


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def ws_recv_text(sock):
    """Receive one (text) frame from the server. Server frames are unmasked."""
    head = _recv_exact(sock, 2)
    if not head:
        return None
    opcode = head[0] & 0x0F
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if head[1] & 0x80:  # masked (servers shouldn't, but handle anyway)
        mask = _recv_exact(sock, 4)
        payload = _recv_exact(sock, length) or b""
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    else:
        payload = _recv_exact(sock, length) or b"" if length else b""
    if opcode == 0x8:
        return None
    return payload.decode("utf-8", "replace")


def ws_send_close(sock):
    try:
        sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))  # masked empty close
    except OSError:
        pass


def stream_ws(host, port, count):
    sock = ws_connect(host, port)
    print("connected to ws://%s:%d/ws" % (host, port))
    try:
        for i in range(count):
            text = ws_recv_text(sock)
            if text is None:
                print("connection closed")
                break
            snap = json.loads(text)
            objs = snap.get("objects", [])
            allies = sum(1 for o in objs if o.get("relation") == 1)
            enemies = sum(1 for o in objs if o.get("relation") == 2)
            print("#%02d  active=%s  map=%s  ships=%d (ally=%d enemy=%d)  ts=%s" % (
                i + 1, snap.get("active"),
                (snap.get("map") or {}).get("name"),
                len(objs), allies, enemies, snap.get("ts")))
    finally:
        ws_send_close(sock)
        sock.close()


def poll_rest(host, port):
    url = "http://%s:%d/map_obj.json" % (host, port)
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    print("GET %s -> %d objects" % (url, len(data)))
    for o in data[:5]:
        print("  %-12s rel=%s pos=(%.0f, %.0f) hp=%.0f%%" % (
            o.get("name"), o.get("relation"),
            o.get("x") or 0, o.get("z") or 0,
            (o.get("hpRatio") or 0) * 100))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8111)
    ap.add_argument("--messages", type=int, default=5)
    ap.add_argument("--rest", action="store_true", help="poll REST once instead of WS")
    args = ap.parse_args()
    if args.rest:
        poll_rest(args.host, args.port)
    else:
        stream_ws(args.host, args.port, args.messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
