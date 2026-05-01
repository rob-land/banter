"""Banter — Faye/Bayeux WebSocket push client for GroupMe real-time events."""

import json
import os
import ssl
import socket
import time
import base64
import zlib
import hashlib
import threading
import urllib.request
import urllib.parse

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GLib

from .constants import GROUPME_PUSH, APP_VERSION, dbg


class GroupMePush:
    """Bayeux/Faye WebSocket push client for GroupMe.

    Uses a persistent WebSocket connection to wss://push.groupme.com/faye.
    The Faye protocol JSON messages are the same as the long-polling variant
    but are sent/received over a single WebSocket instead of repeated HTTP
    POST requests. This gives true real-time delivery with no polling overhead.

    No external libraries required — WebSocket framing is implemented using
    only Python stdlib (socket, ssl, base64, hashlib).

    Protocol:
      1. TCP+TLS connect to push.groupme.com:443
      2. HTTP Upgrade handshake → 101 Switching Protocols
      3. Send Faye /meta/handshake    → get clientId
      4. Send Faye /meta/subscribe    → /user/<user_id>
      5. Send Faye /meta/connect      (connectionType: websocket)
      6. Server pushes events as WebSocket text frames indefinitely
      7. Re-connect with exponential back-off on any error
    """

    WS_HOST = "push.groupme.com"
    WS_PATH = "/faye"
    WS_PORT = 443

    def __init__(self, token: str, user_id: str,
                 on_event, on_error=None):
        self._token      = token
        self._user_id    = str(user_id)
        self._on_event   = on_event
        self._on_error   = on_error or (lambda m: dbg("push error: %s", m))
        self._client_id  = None
        self._msg_id     = 0
        self._running    = False
        self._thread     = None
        self._sock       = None          # raw ssl socket
        self._group_subs : set = set()
        # DM subscriptions, keyed by the underscore-joined sorted-user-id
        # pair (e.g. "118175628_139889463") — the suffix of the
        # /direct_message/<key> Faye channel each DM uses for typing.
        self._dm_subs    : set = set()
        # Frame-send lock so the worker thread's draining writes don't
        # interleave bytes with main-thread publish() calls (typing
        # pulses). Two concurrent sendalls on the same socket can corrupt
        # the WebSocket framing.
        self._send_lock  = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the background thread to stop. Does not touch the socket
        directly — the thread owns the socket and closes it in its finally block
        once it sees _running=False and the current recv unblocks."""
        self._running = False

    def subscribe_group(self, group_id: str):
        """Add `group_id` to the subscription set and, if the WS is
        already up, fire-and-forget the /meta/subscribe frame so events
        start flowing on the *current* connection (not just on the next
        reconnect).

        We deliberately don't read the ack here — the worker thread owns
        the recv side, and racing two recv()s on the same socket would
        corrupt frames. The worker's recv loop will pick up the
        subscribe response and harmlessly drop it (it doesn't match the
        /user/, /group/, or /direct_message/ event-forwarding filter)."""
        gid = str(group_id)
        is_new = gid not in self._group_subs
        self._group_subs.add(gid)
        if is_new:
            self._live_subscribe(f"/group/{gid}")

    def subscribe_dm(self, channel_key: str):
        """Add a /direct_message/<channel_key> subscription so DM typing
        events for that conversation are delivered. `channel_key` is the
        underscore-joined sorted-user-id pair, e.g. "118175628_139889463".

        Same fire-and-forget pattern as subscribe_group — see that
        method's docstring for why we don't recv the ack here."""
        key = str(channel_key)
        is_new = key not in self._dm_subs
        self._dm_subs.add(key)
        if is_new:
            self._live_subscribe(f"/direct_message/{key}")

    def _live_subscribe(self, channel: str):
        """Send a /meta/subscribe frame on the live socket if one is up.
        No-op when offline (the next reconnect picks up the channel from
        _group_subs / _dm_subs)."""
        if not (self._client_id and self._sock is not None):
            return
        try:
            self._faye_send([{
                "channel"     : "/meta/subscribe",
                "clientId"    : self._client_id,
                "subscription": channel,
                "id"          : self._next_id(),
                "ext"         : {
                    "access_token": self._token,
                    "timestamp"   : int(time.time()),
                },
            }])
        except Exception as e:
            dbg("push: live subscribe %s failed: %s", channel, e)

    def publish(self, channel: str, data: dict) -> bool:
        """Publish a Faye event to `channel`. Used for typing pulses.

        Best-effort: silently drops the publish if we aren't currently
        connected. The caller is expected to send pulses on a timer, so
        a missed pulse just means the typing indicator will time out a
        few seconds later on the receivers — acceptable."""
        if not self._client_id or self._sock is None:
            return False
        try:
            self._faye_send([{
                "channel" : channel,
                "data"    : data,
                "clientId": self._client_id,
                "id"      : self._next_id(),
                "ext"     : {"access_token": self._token},
            }])
            return True
        except Exception as e:
            dbg("push: publish to %s failed: %s", channel, e)
            return False

    def publish_typing_group(self, group_id: str):
        """Emit a 'typing' pulse to /group/{gid}. Each pulse is a fresh
        publish — receivers maintain their own sliding-timeout window
        and auto-clear when no follow-up arrives."""
        return self.publish(f"/group/{group_id}", {
            "type"    : "typing",
            "user_id" : self._user_id,
            "started" : int(time.time() * 1000),
        })

    def publish_typing_dm(self, channel_key: str):
        """Emit a 'typing' pulse to /direct_message/<channel_key>. Same
        decay convention as publish_typing_group."""
        return self.publish(f"/direct_message/{channel_key}", {
            "type"    : "typing",
            "user_id" : self._user_id,
            "started" : int(time.time() * 1000),
        })

    # ── WebSocket low-level ─────────────────────────────────────────
    def _ws_connect(self):
        """Open a TLS TCP socket and perform the HTTP WebSocket upgrade."""
        raw = socket.create_connection((self.WS_HOST, self.WS_PORT), timeout=15)
        ctx = ssl.create_default_context()
        self._sock = ctx.wrap_socket(raw, server_hostname=self.WS_HOST)

        key_bytes = os.urandom(16)
        ws_key    = base64.b64encode(key_bytes).decode()
        # Advertise permessage-deflate so we can decompress what the server sends
        request   = (
            f"GET {self.WS_PATH} HTTP/1.1\r\n"
            f"Host: {self.WS_HOST}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Extensions: permessage-deflate\r\n"
            f"User-Agent: GroupMe-GNOME/{APP_VERSION}\r\n"
            f"\r\n"
        )
        self._sock.sendall(request.encode())

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket upgrade: connection closed")
            resp += chunk

        status_line = resp.split(b"\r\n")[0].decode()
        if "101" not in status_line:
            raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")

        expected_accept = base64.b64encode(
            hashlib.sha1(
                (ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        if expected_accept not in resp.decode(errors="replace"):
            raise ConnectionError("WebSocket upgrade: bad Sec-WebSocket-Accept")

        # Check whether server accepted permessage-deflate
        self._deflate = b"permessage-deflate" in resp
        dbg("push: WebSocket connected (deflate=%s)", self._deflate)

    def _ws_send(self, data: str):
        """Send a masked text WebSocket frame.

        Locked so a concurrent main-thread publish() can't interleave
        bytes with a worker-thread send (e.g. /meta/connect). Two raw
        sendalls on the same socket would otherwise corrupt the frame
        boundary."""
        payload = data.encode("utf-8")
        length  = len(payload)
        mask    = os.urandom(4)
        masked  = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        header = bytearray()
        header.append(0x81)   # FIN=1, opcode=1 (text)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        header += mask
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def _ws_recv(self) -> bytes | None:
        """Receive one WebSocket frame. Returns raw payload bytes or None on close."""
        header = self._recv_exact(2)
        if header is None:
            return None

        # RSV1 set = permessage-deflate compressed frame
        rsv1   = (header[0] & 0x40) != 0
        opcode = header[0] & 0x0F
        masked = (header[1] & 0x80) != 0
        length = header[1] & 0x7F

        if opcode == 0x8:     # connection close
            return None
        if opcode == 0x9:     # ping → pong
            body = self._recv_exact(length) if length else b""
            self._ws_pong(body)
            return self._ws_recv()
        if opcode == 0xA:     # pong — ignore
            if length:
                self._recv_exact(length)
            return self._ws_recv()

        if length == 126:
            ext = self._recv_exact(2)
            if ext is None: return None
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = self._recv_exact(8)
            if ext is None: return None
            length = int.from_bytes(ext, "big")

        mask_key = b""
        if masked:
            mask_key = self._recv_exact(4)
            if mask_key is None: return None

        payload = self._recv_exact(length)
        if payload is None:
            return None

        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        # Decompress permessage-deflate frames (RSV1=1)
        if rsv1:
            # RFC 7692: append 4 sync bytes then decompress with raw deflate
            payload = zlib.decompressobj(-zlib.MAX_WBITS).decompress(
                payload + b"\x00\x00\xff\xff")

        # Accept text (0x1), binary (0x2), and continuation (0x0) frames
        if opcode in (0x0, 0x1, 0x2):
            return payload
        return None

    def _recv_exact(self, n: int) -> bytes | None:
        """Read exactly n bytes from the socket."""
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _ws_pong(self, body: bytes = b""):
        try:
            frame = bytes([0x8A, len(body)]) + body
            self._sock.sendall(frame)
        except Exception:
            pass

    def _ws_close(self):
        try:
            self._sock.sendall(bytes([0x88, 0x00]))
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    # ── Faye message helpers ────────────────────────────────────────
    def _next_id(self) -> str:
        self._msg_id += 1
        return str(self._msg_id)

    def _faye_send(self, payload: list):
        self._ws_send(json.dumps(payload))

    def _faye_recv(self) -> list | None:
        """Receive one frame. Returns parsed list, empty list on bad JSON,
        or None when the socket is closed."""
        raw = self._ws_recv()
        if raw is None:
            return None          # socket closed — signal reconnect
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            dbg("push: undecodable frame (len=%d), skipping", len(raw))
            return []            # skip frame, keep connection

    # ── Faye session ────────────────────────────────────────────────
    def _handshake(self) -> bool:
        self._faye_send([{
            "channel"                : "/meta/handshake",
            "version"                : "1.0",
            "supportedConnectionTypes": ["websocket"],
            "id"                     : self._next_id(),
        }])
        # _faye_recv returns None when the socket is closed mid-recv
        # (network drop). Treat that as a failed handshake — caller
        # will reconnect.
        frames = self._faye_recv() or []
        for msg in frames:
            if msg.get("channel") == "/meta/handshake":
                if msg.get("successful"):
                    self._client_id = msg["clientId"]
                    dbg("push: handshake ok clientId=%s", self._client_id)
                    return True
                dbg("push: handshake refused: %s", msg)
                return False
        return False

    def _subscribe(self, channel: str) -> bool:
        self._faye_send([{
            "channel"     : "/meta/subscribe",
            "clientId"    : self._client_id,
            "subscription": channel,
            "id"          : self._next_id(),
            "ext"         : {
                "access_token": self._token,
                "timestamp"   : int(time.time()),
            },
        }])
        # Same null-safety as _handshake: socket may be closed mid-recv.
        frames = self._faye_recv() or []
        for msg in frames:
            if msg.get("channel") == "/meta/subscribe":
                ok = msg.get("successful", False)
                dbg("push: subscribe %s → %s", channel, ok)
                return ok
        return False

    def _connect_ws(self):
        """Send the /meta/connect frame that puts the server in streaming mode."""
        self._faye_send([{
            "channel"       : "/meta/connect",
            "clientId"      : self._client_id,
            "connectionType": "websocket",
            "id"            : self._next_id(),
        }])

    # ── Main loop ───────────────────────────────────────────────────
    def _run(self):
        """Main loop — implements the Bayeux WebSocket request/response cycle.

        The GroupMe push server uses a "long-poll over WebSocket" pattern:
          1. Open a new WebSocket connection
          2. Handshake → get clientId
          3. Subscribe to channels
          4. Send ONE /meta/connect — server holds it, pushes queued events,
             then closes the WebSocket when the hold expires (~10–30 s)
          5. Immediately open a new WebSocket (no sleep) and repeat from step 1

        The server closing the connection is NORMAL. It is NOT an error.
        Only genuine failures (TCP error, bad handshake, etc.) trigger backoff.
        """
        fail_count = 0
        MAX_FAILS  = 5

        while self._running:
            normal_close = False
            try:
                self._ws_connect()
            except Exception as e:
                fail_count += 1
                backoff = min(2 ** fail_count, 60)
                dbg("push: connect failed (%s), retry in %ds", e, backoff)
                if fail_count <= MAX_FAILS:
                    GLib.idle_add(self._on_error,
                                   f"Push connection failed – retrying in {backoff}s")
                time.sleep(backoff)
                continue

            try:
                if not self._handshake():
                    raise RuntimeError("Handshake failed")

                if not self._subscribe(f"/user/{self._user_id}"):
                    raise RuntimeError("User subscribe failed")

                for gid in list(self._group_subs):
                    self._subscribe(f"/group/{gid}")
                for ck in list(self._dm_subs):
                    self._subscribe(f"/direct_message/{ck}")

                # Send the single /meta/connect that starts the event stream
                self._connect_ws()

                # Drain all frames until the server closes the connection
                while self._running:
                    frames = self._faye_recv()

                    if frames is None:
                        # Server closed the WebSocket — this is expected and normal
                        normal_close = True
                        break

                    for ev in frames:
                        advice = ev.get("advice", {})
                        if advice.get("reconnect") == "handshake":
                            # Server is asking us to fully re-authenticate
                            dbg("push: server requested re-handshake")
                            normal_close = True
                            break

                        channel = ev.get("channel", "")
                        data    = ev.get("data")
                        # Forward /user/{uid} (line.create, reactions,
                        # edits, etc.), /group/{gid} (group typing +
                        # group-only signals), and /direct_message/<key>
                        # (DM typing). ChatView's _on_push_event is
                        # robust to duplicate line.create deliveries.
                        if data and (channel.startswith("/user/") or
                                     channel.startswith("/group/") or
                                     channel.startswith("/direct_message/")):
                            GLib.idle_add(self._on_event, data)

            except Exception as e:
                dbg("push: session error: %s", e)
                fail_count += 1
                if fail_count <= MAX_FAILS:
                    GLib.idle_add(self._on_error,
                                   f"Push error ({e}) – reconnecting")
                time.sleep(min(2 ** fail_count, 30))
            else:
                # No exception — reset failure counter on a successful cycle
                if normal_close:
                    fail_count = 0
            finally:
                try:
                    self._ws_close()
                except Exception:
                    pass
                self._sock      = None
                self._client_id = None

            # Normal close: reconnect immediately (no sleep).
            # Only sleep after genuine failures (handled above via fail_count).
            # This is the tight reconnect loop the Bayeux protocol expects.


