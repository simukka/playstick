"""The WebSocket layer: the handshake, the framing, and what a server must
refuse.

The framing tests run a ws.Connection over a socketpair, with frames built
here the way a browser builds them: masked, as RFC 6455 requires of a client.
The handshake tests go through the real handler on a real port, with a fake
game runner standing behind /api/game/socket.
"""

import base64
import os
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import ApiTest                                 # noqa: E402
from playstick import ws                                    # noqa: E402

MASK = b"\x11\x22\x33\x44"


def client_frame(opcode, payload=b"", fin=True, masked=True):
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    bit = 0x80 if masked else 0
    if n < 126:
        head.append(bit | n)
    elif n < 65536:
        head.append(bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", n)
    if not masked:
        return bytes(head) + payload
    body = bytes(b ^ MASK[i & 3] for i, b in enumerate(payload))
    return bytes(head) + MASK + body


def read_server_frame(sock):
    head = _exact(sock, 2)
    n = head[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", _exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _exact(sock, 8))[0]
    return head[0] & 0x0F, _exact(sock, n)


def _exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


class Framing(unittest.TestCase):
    def setUp(self):
        self.server_end, self.phone = socket.socketpair()
        self.phone.settimeout(5)
        self.addCleanup(self.server_end.close)
        self.addCleanup(self.phone.close)
        # Unbuffered, like the handler's own wfile, so nothing is left in a
        # buffer to be flushed into a closed socket at garbage collection.
        self.conn = ws.Connection(self.server_end.makefile("rb"),
                                  self.server_end.makefile("wb", buffering=0),
                                  self.server_end)
        self.addCleanup(self.conn.drain)
        self.addCleanup(self.conn.close)

    def test_the_rfc_example_key_gets_the_rfc_example_answer(self):
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_a_masked_message_arrives_unmasked(self):
        self.phone.sendall(client_frame(ws.BINARY, b"\x00\x00\x11"))
        self.assertEqual(self.conn.recv(), (ws.BINARY, b"\x00\x00\x11"))

    def test_lengths_past_the_short_form_are_read(self):
        for size in (125, 126, 300, 70000 // 2):
            with self.subTest(size=size):
                self.phone.sendall(client_frame(ws.TEXT, b"x" * size))
                self.assertEqual(self.conn.recv(), (ws.TEXT, b"x" * size))

    def test_a_fragmented_message_is_put_back_together(self):
        self.phone.sendall(client_frame(ws.TEXT, b"hel", fin=False))
        self.phone.sendall(client_frame(ws.PING, b"p"))
        self.phone.sendall(client_frame(ws.CONTINUATION, b"lo"))
        self.assertEqual(self.conn.recv(), (ws.TEXT, b"hello"))
        # The ping in the middle was answered on the way past.
        self.assertEqual(read_server_frame(self.phone), (ws.PONG, b"p"))

    def test_an_unmasked_client_frame_closes_the_socket(self):
        self.phone.sendall(client_frame(ws.TEXT, b"hi", masked=False))
        with self.assertRaises(ws.Closed):
            self.conn.recv()

    def test_a_frame_bigger_than_any_message_is_refused_unread(self):
        head = bytes([0x82, 0x80 | 127]) + struct.pack("!Q", 1 << 40)
        self.phone.sendall(head + MASK)
        with self.assertRaises(ws.Closed):
            self.conn.recv()

    def test_a_close_is_answered_and_ends_the_conversation(self):
        self.phone.sendall(client_frame(ws.CLOSE, struct.pack("!H", 1000)))
        with self.assertRaises(ws.Closed):
            self.conn.recv()
        self.assertEqual(read_server_frame(self.phone)[0], ws.CLOSE)

    def test_text_and_sound_go_out_as_frames(self):
        self.conn.send_text({"role": "p1"})
        self.assertEqual(read_server_frame(self.phone),
                         (ws.TEXT, b'{"role": "p1"}'))
        self.conn.send_binary(b"\x01\x02")
        self.assertEqual(read_server_frame(self.phone), (ws.BINARY, b"\x01\x02"))

    def test_a_phone_that_falls_behind_loses_old_sound_not_messages(self):
        """The writer is stalled by never reading. Sound piles up past the
        queue's length and the oldest goes; the role message does not."""
        gate = threading.Event()
        real = self.conn._wfile.write

        def slow(frame):
            gate.wait(5)
            return real(frame)

        self.conn._wfile.write = slow
        for i in range(ws.AUDIO_QUEUE * 3):
            self.conn.send_binary(bytes([i]))
        self.conn.send_text({"role": "listener"})
        gate.set()
        self.assertGreater(self.conn.dropped, 0)
        # Read until the role message, however the frames were interleaved.
        sound = 0
        while True:
            kind, payload = read_server_frame(self.phone)
            if kind == ws.TEXT:
                self.assertEqual(payload, b'{"role": "listener"}')
                break
            sound += 1
        self.conn.close()
        while True:
            kind, _ = read_server_frame(self.phone)
            if kind == ws.CLOSE:
                break
            sound += 1
        # The queue's worth, and perhaps the one frame the writer already held.
        self.assertLessEqual(sound, ws.AUDIO_QUEUE + 1)


class EchoGame:
    """Stands behind /api/game/socket: echoes one message, then hangs up."""

    def status(self):
        return None

    def serve(self, conn):
        try:
            kind, payload = conn.recv()
            conn.send_text({"echo": payload.decode()})
        except ws.Closed:
            pass
        conn.close()
        conn.drain()


class Handshake(ApiTest):
    def setUp(self):
        super().setUp()
        self.server.RequestHandlerClass.game = EchoGame()

    def upgrade(self, **headers):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        fields = {"Host": "stick", "Upgrade": "websocket",
                  "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                  "Sec-WebSocket-Key":
                      base64.b64encode(os.urandom(16)).decode()}
        fields.update(headers)
        request = "GET /api/game/socket HTTP/1.1\r\n" + "".join(
            "%s: %s\r\n" % kv for kv in fields.items() if kv[1] is not None)
        sock.sendall((request + "\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(1024)
            if not chunk:
                break
            reply += chunk
        status = int(reply.split(b" ", 2)[1]) if reply else 0
        return sock, status, reply

    def test_a_phone_upgrades_and_is_talked_to(self):
        sock, status, reply = self.upgrade()
        self.assertEqual(status, 101)
        self.assertIn(b"Sec-WebSocket-Accept:", reply)
        sock.sendall(client_frame(ws.TEXT, b"hello"))
        self.assertEqual(read_server_frame(sock),
                         (ws.TEXT, b'{"echo": "hello"}'))

    def test_a_request_that_is_not_an_upgrade_is_refused(self):
        _sock, status, _ = self.upgrade(Upgrade=None, Connection="keep-alive")
        self.assertEqual(status, 400)

    def test_an_old_protocol_version_is_refused(self):
        _sock, status, _ = self.upgrade(**{"Sec-WebSocket-Version": "8"})
        self.assertEqual(status, 400)

    def test_a_key_that_is_not_sixteen_bytes_is_refused(self):
        _sock, status, _ = self.upgrade(**{"Sec-WebSocket-Key": "c2hvcnQ="})
        self.assertEqual(status, 400)

    def test_a_daemon_without_games_says_so(self):
        self.server.RequestHandlerClass.game = None
        _sock, status, _ = self.upgrade()
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
