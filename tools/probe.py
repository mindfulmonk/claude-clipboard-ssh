#!/usr/bin/env python3
"""
Probe kitty's OSC 52 / OSC 5522 clipboard protocol.

Usage:
  ./probe.py 52         # read text clipboard via OSC 52
  ./probe.py 5522       # read image/png via OSC 5522
  ./probe.py 5522 image/png,image/jpeg,text/plain
  ./probe.py raw '<ESC>]5522;type=read;...<ESC>\\' --- send arbitrary bytes

Reads/writes /dev/tty directly in raw mode so it works even when stdin/stdout
are piped. Dumps every byte received with a 2s idle timeout, then prints a
structured parse.
"""
import base64
import os
import select
import sys
import termios
import tty

IDLE_TIMEOUT = 1.0    # idle gap once bytes have started arriving
FIRST_BYTE_TIMEOUT = 30.0  # how long to wait for kitty to respond at all
                          # (allows time for clipboard-access popup on the Mac)
HARD_TIMEOUT = 60.0   # absolute cap


def open_tty():
    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    return fd


def send(fd, data: bytes):
    sys.stderr.write(f"--> sending {len(data)} bytes: {data!r}\n")
    os.write(fd, data)


def drain(fd, idle=IDLE_TIMEOUT, first=FIRST_BYTE_TIMEOUT, hard=HARD_TIMEOUT) -> bytes:
    """Read until either:
       - no bytes arrive within `first` seconds (kitty never replied), or
       - bytes started, then went idle for `idle` seconds, or
       - `hard` seconds total elapsed.
    """
    import time
    buf = bytearray()
    deadline = time.monotonic() + hard
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            sys.stderr.write("[hard timeout]\n")
            break
        wait = idle if buf else first
        r, _, _ = select.select([fd], [], [], min(wait, remaining))
        if not r:
            if buf:
                sys.stderr.write(f"[idle {idle}s, stopping]\n")
            else:
                sys.stderr.write(f"[no bytes after {first}s — kitty never answered]\n")
            break
        chunk = os.read(fd, 65536)
        if not chunk:
            sys.stderr.write("[EOF]\n")
            break
        buf.extend(chunk)
        if os.environ.get("PROBE_VERBOSE"):
            sys.stderr.write(f"[+{len(chunk)}B, total {len(buf)}]\n")
    return bytes(buf)


def hexdump(b: bytes, max_bytes: int = 512) -> str:
    shown = b[:max_bytes]
    out = []
    for i in range(0, len(shown), 16):
        row = shown[i:i+16]
        hexs = " ".join(f"{x:02x}" for x in row)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
        out.append(f"  {i:04x}  {hexs:<48}  {asc}")
    if len(b) > max_bytes:
        out.append(f"  ... ({len(b) - max_bytes} more bytes)")
    return "\n".join(out)


def split_osc(resp: bytes):
    """Yield OSC payloads — split on ESC] ... ST (ESC \\) or BEL."""
    i = 0
    while i < len(resp):
        if resp[i:i+2] == b"\x1b]":
            # find terminator
            j = i + 2
            while j < len(resp):
                if resp[j:j+2] == b"\x1b\\":
                    yield resp[i+2:j]
                    i = j + 2
                    break
                if resp[j] == 0x07:
                    yield resp[i+2:j]
                    i = j + 1
                    break
                j += 1
            else:
                # unterminated
                yield resp[i+2:]
                return
        else:
            i += 1


def probe_osc52(fd):
    # ESC ] 52 ; c ; ? ST  -- request clipboard
    send(fd, b"\x1b]52;c;?\x1b\\")
    resp = drain(fd)
    sys.stderr.write(f"<-- got {len(resp)} bytes\n{hexdump(resp)}\n")
    for payload in split_osc(resp):
        sys.stderr.write(f"  OSC payload: {payload!r}\n")
        # 52;c;<base64>
        parts = payload.split(b";", 2)
        if len(parts) == 3 and parts[0] == b"52":
            try:
                decoded = base64.b64decode(parts[2])
                sys.stderr.write(f"    decoded text: {decoded!r}\n")
            except Exception as e:
                sys.stderr.write(f"    base64 decode failed: {e}\n")


def parse_data_packet(payload: bytes):
    """A DATA packet looks like:
       b'5522;type=read:status=DATA:mime=<b64-mime>;<b64-data>'
       Returns (mime, raw_bytes) or None if not a DATA packet.
    """
    if not payload.startswith(b"5522;"):
        return None
    # split on the first ';' AFTER the metadata block
    # metadata ends at the ';' separating meta from payload
    # meta keys are colon-separated: type=read:status=DATA:mime=...
    head, _, data_b64 = payload.partition(b";")  # eat '5522;'
    meta, sep, data_b64 = data_b64.partition(b";")
    if not sep:
        return None
    # parse meta key=val pairs separated by ':'
    fields = dict(item.split(b"=", 1) for item in meta.split(b":") if b"=" in item)
    if fields.get(b"status") != b"DATA":
        return None
    mime_b64 = fields.get(b"mime", b"")
    try:
        mime = base64.b64decode(mime_b64).decode("ascii", errors="replace")
        raw = base64.b64decode(data_b64)
    except Exception as e:
        sys.stderr.write(f"  decode error: {e}\n")
        return None
    return mime, raw


def probe_osc5522(fd, mime_list, save_prefix=None):
    mime_blob = " ".join(mime_list).encode()
    b64 = base64.b64encode(mime_blob).decode()
    seq = f"\x1b]5522;type=read;{b64}\x1b\\".encode()
    send(fd, seq)
    resp = drain(fd)
    sys.stderr.write(f"<-- got {len(resp)} bytes total\n")

    by_mime = {}        # mime -> bytearray
    status_lines = []
    n_data = 0
    for payload in split_osc(resp):
        if b"status=DATA" in payload:
            parsed = parse_data_packet(payload)
            if parsed:
                mime, raw = parsed
                by_mime.setdefault(mime, bytearray()).extend(raw)
                n_data += 1
        else:
            status_lines.append(payload.decode("ascii", errors="replace"))

    sys.stderr.write(f"--- status packets ({len(status_lines)}):\n")
    for s in status_lines:
        sys.stderr.write(f"    {s}\n")
    sys.stderr.write(f"--- DATA chunks: {n_data}, MIME types received: {list(by_mime)}\n")
    for mime, buf in by_mime.items():
        sys.stderr.write(f"    {mime}: {len(buf)} bytes  head={bytes(buf[:16]).hex()}\n")
        if save_prefix:
            safe = mime.replace("/", "_")
            path = f"{save_prefix}.{safe}"
            with open(path, "wb") as f:
                f.write(buf)
            sys.stderr.write(f"      -> saved {path}\n")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    mode = sys.argv[1]

    fd = open_tty()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if mode == "52":
            probe_osc52(fd)
        elif mode == "5522":
            mimes = sys.argv[2].split(",") if len(sys.argv) > 2 else ["image/png"]
            save = sys.argv[3] if len(sys.argv) > 3 else None
            probe_osc5522(fd, mimes, save_prefix=save)
        elif mode == "raw":
            data = sys.argv[2].encode().decode("unicode_escape").encode("latin-1")
            send(fd, data)
            resp = drain(fd)
            sys.stderr.write(f"<-- {len(resp)} bytes\n{hexdump(resp)}\n")
        else:
            print(f"unknown mode: {mode}")
            sys.exit(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)


if __name__ == "__main__":
    main()
