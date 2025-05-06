import argparse
import mmap
import os
import os.path
import socket as sock
import sys

from datetime import datetime
from inotify_simple import INotify, flags as IFlags
from select import select

def readmemoryviewinto(source):
    def copy_into(target):
        nonlocal source
        copy_size = min(len(source), len(target))
        target[:copy_size] = source[:copy_size]
        source = source[copy_size:]
        return copy_size
    return copy_into


class PipeWriter:
    def __init__(self, size = 2<<21):
        self.buf = memoryview(bytearray(size))
        self.buf_start = 0
        self.buf_end = 0

    def capacity(self):
        buf_len = len(self.buf)
        buf_start = self.buf_start
        buf_end = self.buf_end
        if buf_end < buf_start:
            buf_end += buf_len
        return buf_len - (buf_end - buf_start)

    def stats(self):
        buf_len = len(self.buf)
        buf_start = self.buf_start
        buf_end = self.buf_end
        if buf_end < buf_start:
            buf_end += buf_len
        used = buf_end - buf_start
        return (used, buf_len - used)

    def has_data(self):
        return self.buf_start != self.buf_end

    def has_free(self):
        return (self.buf_end + 1) % len(self.buf) != self.buf_start

    def read_buf(self, source, readinto=None):
        readinto = readinto or (lambda x: x.readinto)
        buf_len = len(self.buf)
        if self.buf_start == self.buf_end:
            start = self.buf_start = self.buf_end = 0
            end = buf_len
        if self.buf_start <= self.buf_end:
            start = self.buf_end
            end = buf_len
        else:
            start = self.buf_end
            end = self.buf_start
        read_len = readinto(source)(self.buf[start:end])
        self.buf_end = (self.buf_end + read_len) % buf_len
        return read_len

    def write_buf(self, target, write=None):
        write = write or (lambda x: x.write)
        buf_len = len(self.buf)
        start = self.buf_start
        if self.buf_start == self.buf_end:
            return 0
        elif self.buf_start < self.buf_end:
            end = self.buf_end
        else:
            end = buf_len
        write_len = write(target)(self.buf[start:end])
        self.buf_start = (self.buf_start + write_len) % buf_len
        return write_len

    def pipe(self, source, target, readinto=None, write=None, flush=True):
        is_memoryview = isinstance(source, memoryview)
        ready = False

        while True:
            has_free = self.has_free()
            has_data = self.has_data()
            if is_memoryview and not ready:
                ready = not bool(source)
                if has_free and not ready:
                    copied = self.read_buf(source, readinto=readmemoryviewinto)
                    source = source[copied:]
                    has_data = bool(copied)
                    continue
            do_read = has_free and not (ready or is_memoryview)
            do_write = (has_data and flush) or ((not has_free) and (not ready))
            if not (do_read or do_write):
                return
            (readable, writable, _) = select([source] if do_read else [], [target] if do_write else [], [])
            if source in readable:
                if self.read_buf(source, readinto=readinto) == 0:
                    ready = True
            if target in writable:
                self.write_buf(target, write=write)

    def write_bytes(self, target, source, flush=False, write=None):
        is_memoryview = isinstance(source, memoryview)
        assert is_memoryview or isinstance(source, bytes) or isinstance(source, bytearray), f"Invalid bytes to write_bytes: {source}"
        return self.pipe(source if is_memoryview else memoryview(source), target, flush=flush, write=write)

def write_all(target, data, write=lambda x: x.write):
    total_bytes_written = 0
    while bytes_written := write(target)(data[total_bytes_written:]):
        total_bytes_written += bytes_written
    return total_bytes_written

SD_LISTEN_FDS_START=3

def _main(filename, work_dir, boundary, socket):
    inotify = INotify()
    event_file_moved = None
    work_dir = os.path.abspath(work_dir)
    work_dir_name = os.path.basename(work_dir)
    working_path = os.path.join(work_dir, os.path.basename(filename))
    working_path_parent = os.path.abspath(os.path.join(work_dir, ".."))
    print(f"Watching for new directories in {working_path_parent}")
    event_path_created = inotify.add_watch(working_path_parent, IFlags.CREATE | IFlags.ONLYDIR)
    writer = PipeWriter()
    sd_listen_start = os.environ.get("LISTEN_FDS")
    single = True
    socket_send = None
    if socket == 'systemd':
        if sd_listen_start and int(sd_listen_start) >= 1:
            socket = sock.fromfd(int(SD_LISTEN_FDS_START), sock.AF_INET, sock.SOCK_STREAM | sock.SOCK_NONBLOCK)
            socket_send = lambda x: x.send
            socket.setsockopt(sock.SOL_SOCKET, sock.SO_SNDBUF, 16384)
            socket.setsockopt(sock.IPPROTO_TCP, sock.TCP_NODELAY, True)
            valid_headers = {b'GET /?action=stream': False, b'GET /?action=snapshot': True}
            min_headers_len = max([len(x) for x in valid_headers.keys()])
            headers = memoryview(bytearray(1024))
            headers_len = 0
            while headers_len < min_headers_len:
                select([socket], [], [])
                bytes_read = socket.recv_into(headers[headers_len:])
                if bytes_read == 0:
                    print("Not enough headers")
                    exit(1)
                headers_len += bytes_read
            query = headers[:headers_len]
            for (valid, header_single) in valid_headers.items():
                if query[0:len(valid)] == valid:
                    single = header_single
                    break
            else:
                print(f"Invalid headers: {query.decode('utf-8')}")
                exit(1)
        else:
            print("Start this with systemd")
            exit(1)
    elif socket == 'stdio':
        socket = os.fdopen(sys.stderr.fileno(), "wb", buffering=0)
    else:
        assert False, "Invalid socket choice"
    formatted_boundary = b""

    def update_file_inotify():
        nonlocal event_file_moved
        if event_file_moved is not None:
            try:
                inotify.rm_watch(event_file_moved)
            except:
                pass
        if not os.path.isdir(work_dir):
            for event in inotify.read():
                if event.wd == event_path_created and event.name == work_dir_name:
                    break
        event_file_moved = inotify.add_watch(work_dir, IFlags.MOVED_TO | IFlags.ONLYDIR)

    with socket as client:
        send_all = lambda data: writer.write_bytes(client, data, write=socket_send)
        send_all(b"HTTP/1.1 200 OK\r\n")
        send_all(b"Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n")
        send_all(b"X-Accel-Buffering: no\r\n")
        if single:
            send_all(b"Content-Type: image/jpeg\r\n")
        else:
            send_all(b"Content-Type: multipart/x-mixed-replace;boundary=\"")
            send_all(boundary)
            send_all(b"\"\r\n")
            formatted_boundary = b"\r\n--" + boundary + b"\r\nContent-Type: image/jpeg\r\n"
        wait_for_file = False
        update_file_inotify()
        try:
            with open(working_path, "rb") as _:
                wait_for_file = single
        except FileNotFoundError:
            wait_for_file = True
        while True:
            send_all(formatted_boundary)
            while wait_for_file:
                for event in inotify.read():
                    if event.wd == event_file_moved and event.name == filename:
                        break
                    elif event.wd == event_path_created and event.name == work_dir_name:
                        update_file_inotify()
                else:
                    continue
                break
            wait_for_file = True
            try:
                current = open(working_path, "rb", buffering=0)
            except FileNotFoundError:
                continue
            with current as current:
                current.seek(0, os.SEEK_END)
                file_length = current.tell()
                current.seek(0, os.SEEK_SET)
                if single:
                    stats = os.stat(current.fileno())
                    try:
                        created = stats.st_birthtime
                    except AttributeError:
                        created = stats.st_mtime
                    created = datetime.fromtimestamp(created)
                    send_all(f"Content-Disposition: inline; filename=\"snapshot {created}.jpg\"\r\n".encode("utf-8"))
                send_all(f"Content-Length: {file_length}\r\n\r\n".encode('utf-8'))
                writer.pipe(current, client, write=socket_send)
                if single:
                    return

if __name__ == "__main__":
    args = argparse.ArgumentParser(prog="libcamera-lazy-capture")
    args.add_argument('-f', '--filename', type=str, default='current.jpg')
    args.add_argument('--boundary', type=bytes, default=b'MJPG_webcam_dynamic_framerate')
    args.add_argument('--socket', choices=['systemd', 'stdio'], default='stdio')
    args.add_argument('work_dir', type=str)
    args = args.parse_args()
    try:
        _main(**vars(args))
    except (BrokenPipeError, ConnectionResetError):
        print("Client disconnected")

