"""Serial console to the board (USB-UART), stdlib only: works when the network doesn't.

One session at a time. A reader thread keeps the last ~256 KB of output; the web page
streams it over server-sent events and sends lines/keys back.
"""
from __future__ import annotations

import glob
import os
import re
import select
import termios
import threading
import time
import tty

BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600,
         115200: termios.B115200, 230400: termios.B230400, 460800: getattr(termios, 'B460800', termios.B230400),
         921600: getattr(termios, 'B921600', termios.B230400)}
KEEP = 256 * 1024


def lock_owner(dev: str) -> dict | None:
    """Another program (minicom, screen…) holding the port, per the usual UUCP lock files."""
    name = os.path.basename(dev)
    for d in ('/run/lock', '/var/lock'):
        p = os.path.join(d, f'LCK..{name}')
        try:
            pid = int(open(p).read().strip().split()[0])
        except (OSError, ValueError, IndexError):
            continue
        if pid == os.getpid():
            return None
        try:
            cmd = open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\0', b' ').decode().strip()
        except OSError:
            continue          # stale lock: the process is gone
        return {'pid': pid, 'cmd': cmd}
    return None


def list_ports() -> list[dict]:
    by_id = {}
    for link in glob.glob('/dev/serial/by-id/*'):
        by_id[os.path.realpath(link)] = os.path.basename(link)
    out = []
    for dev in sorted(glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*')):
        ident = by_id.get(dev, '')
        note = ''
        m = re.search(r'-if(\d+)', ident)
        if m:   # multi-port debug adapters: the first interface is usually the main (Linux) console
            note = 'probably the Linux console' if m.group(1) == '00' else 'second port (often the M-core / MCU console)'
        out.append({'dev': dev, 'id': ident, 'note': note, 'busy': lock_owner(dev),
                    'access': os.access(dev, os.R_OK | os.W_OK)})
    return out


class SerialSession:
    def __init__(self):
        self.fd = None
        self.dev = None
        self.baud = None
        self.buf = bytearray()
        self.pos = 0                  # absolute offset of buf[0] in the stream
        self.cond = threading.Condition()
        self.thread = None
        self.error = None
        self.autoboot_until = 0.0

    @property
    def is_open(self):
        return self.fd is not None

    def open(self, dev: str, baud: int = 115200, force: bool = False):
        if self.is_open:
            self.close()
        if dev not in {p['dev'] for p in list_ports()}:
            raise ValueError(f'{dev} is not a serial port on this computer')
        owner = lock_owner(dev)
        if owner and not force:
            raise RuntimeError(f"{dev} is in use by another program ({owner['cmd']}, pid {owner['pid']}). "
                               'Close it first (minicom: Ctrl-A then X), or both will miss characters.')
        if not os.access(dev, os.R_OK | os.W_OK):
            raise RuntimeError(f'No permission for {dev}. Add yourself to the dialout group: '
                               'sudo usermod -aG dialout $USER, then log out and in again.')
        fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        speed = BAUDS.get(int(baud), termios.B115200)
        attrs[4] = attrs[5] = speed                                  # ispeed, ospeed
        attrs[2] |= termios.CLOCAL | termios.CREAD                    # ignore modem lines, enable receiver
        attrs[2] &= ~getattr(termios, 'CRTSCTS', 0)                   # no hardware flow control
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)   # no software flow control
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        self.fd, self.dev, self.baud, self.error = fd, dev, int(baud), None
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        fd = self.fd
        while self.fd == fd:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    data = os.read(fd, 4096)
                    if not data:
                        continue
                    with self.cond:
                        self.buf += data
                        if len(self.buf) > KEEP:
                            cut = len(self.buf) - KEEP
                            del self.buf[:cut]; self.pos += cut
                        self.cond.notify_all()
                    if self.autoboot_until and b'=>' in data:
                        self.autoboot_until = 0           # U-Boot prompt reached: stop sending keys
                if self.autoboot_until and time.time() < self.autoboot_until:
                    os.write(fd, b' ')
                elif self.autoboot_until:
                    self.autoboot_until = 0
            except OSError as e:          # unplugged
                self.error = f'serial port closed: {e}'
                with self.cond:
                    self.cond.notify_all()
                self.fd = None
                try:
                    os.close(fd)
                except OSError:
                    pass
                return

    def send(self, data: bytes):
        if not self.is_open:
            raise RuntimeError('serial console is not open')
        os.write(self.fd, data)

    def catch_uboot(self, seconds: int = 30):
        """Keep tapping a key so the next reset stops in U-Boot (stops by itself at the => prompt)."""
        self.autoboot_until = time.time() + seconds

    def read_since(self, offset: int, wait: float = 15.0) -> tuple[int, bytes]:
        with self.cond:
            end = self.pos + len(self.buf)
            if offset >= end and self.is_open:
                self.cond.wait(wait)
                end = self.pos + len(self.buf)
            start = max(offset, self.pos)
            return end, bytes(self.buf[start - self.pos:])

    def close(self):
        fd, self.fd = self.fd, None
        self.autoboot_until = 0
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        with self.cond:
            self.cond.notify_all()

    def state(self) -> dict:
        return {'open': self.is_open, 'dev': self.dev, 'baud': self.baud, 'error': self.error,
                'catching_uboot': bool(self.autoboot_until), 'end': self.pos + len(self.buf)}


SESSION = SerialSession()
