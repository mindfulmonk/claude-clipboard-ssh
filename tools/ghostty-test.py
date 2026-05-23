#!/usr/bin/env python3
"""
ghostty-test.py — minimal OSC 5522 paste test for ghostty.

Run in a ghostty terminal:
    ./ghostty-test.py [output-path]

It will:
  1. Enable mode 5522 on the current tty
  2. Wait for you to paste (Cmd+V)
  3. Snatch the password ghostty sends, fetch the highest-priority MIME
  4. Save the decoded bytes to the output path (default /tmp/ghostty-clip)

Press Ctrl+C to abort.
"""
import base64
import os
import select
import signal
import sys
import termios
import time
import tty


def parse_5522(payload):
    if not payload.startswith(b"5522;"):
        return None
    rest = payload[5:]
    meta, sep, data = rest.partition(b";")
    fields = {}
    for item in meta.split(b":"):
        if b"=" in item:
            k, _, v = item.partition(b"=")
            fields[k.strip()] = v.strip()
    return fields, (data if sep else b"")


def split_complete_osc(buf):
    """Yield (payload_bytes, end_index_after) for each complete OSC in buf."""
    i = 0
    while True:
        start = buf.find(b"\x1b]", i)
        if start == -1:
            return
        end = buf.find(b"\x1b\\", start + 2)
        if end == -1:
            return
        yield bytes(buf[start + 2:end]), end + 2
        i = end + 2


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ghostty-clip"
    prefer = ["image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"]

    tty_fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    old_attrs = termios.tcgetattr(tty_fd)
    tty.setraw(tty_fd)

    def cleanup(_sig=None, _frame=None):
        try:
            os.write(tty_fd, b"\x1b[?5522l")
        except OSError:
            pass
        termios.tcsetattr(tty_fd, termios.TCSADRAIN, old_attrs)
        os.close(tty_fd)
    signal.signal(signal.SIGINT, lambda *_: (cleanup(), os._exit(130)))

    try:
        os.write(tty_fd, b"\x1b[?5522h")
        # Stderr won't render right (we're in raw mode); use the tty
        os.write(tty_fd, b"\r\nMode 5522 enabled. Now paste something (Cmd+V).\r\n")

        buf = bytearray()
        state = "IDLE"
        password = None
        mimes = []
        data_buf = bytearray()
        chosen = None
        start = time.monotonic()

        while True:
            if time.monotonic() - start > 120:
                os.write(tty_fd, b"\r\nTimeout (120s).\r\n")
                return 1

            r, _, _ = select.select([tty_fd], [], [], 30)
            if not r:
                continue
            chunk = os.read(tty_fd, 65536)
            if not chunk:
                return 1
            buf.extend(chunk)

            last_cursor = 0
            for payload, after in split_complete_osc(buf):
                last_cursor = after
                parsed = parse_5522(payload)
                if not parsed:
                    continue
                fields, data = parsed
                status = fields.get(b"status", b"")

                if state == "IDLE" and status == b"OK" and b"password" in fields:
                    password = fields[b"password"]
                    mimes = []
                    state = "PASTE"
                    os.write(tty_fd, b"\r\n[got password]\r\n")
                elif state == "PASTE":
                    if status == b"DATA" and b"mime" in fields:
                        try:
                            mime = base64.b64decode(fields[b"mime"]).decode("ascii")
                            mimes.append(mime)
                        except Exception:
                            pass
                    elif status == b"DONE":
                        os.write(tty_fd, f"\r\n[mimes: {mimes}]\r\n".encode())
                        chosen = next((m for m in prefer if m in mimes), None)
                        if not chosen:
                            os.write(tty_fd, b"\r\nNo preferred MIME on clipboard.\r\n")
                            return 1
                        os.write(tty_fd, f"\r\n[requesting {chosen}]\r\n".encode())
                        mb = base64.b64encode(chosen.encode()).decode("ascii")
                        req = (b"\x1b]5522;type=read:mime=" + mb.encode()
                               + b":password=" + password + b"\x1b\\")
                        os.write(tty_fd, req)
                        state = "DATA"
                elif state == "DATA":
                    if status == b"DATA" and data:
                        data_buf.extend(data)
                    elif status == b"DONE":
                        raw = base64.b64decode(bytes(data_buf))
                        # ensure parent dir exists
                        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
                        with open(out_path, "wb") as f:
                            f.write(raw)
                        os.write(tty_fd,
                                 f"\r\nSaved {len(raw)} bytes to {out_path}\r\n".encode())
                        return 0
                    elif status.startswith(b"E"):
                        os.write(tty_fd,
                                 f"\r\nServer error: {status.decode()}\r\n".encode())
                        return 1
            del buf[:last_cursor]
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
