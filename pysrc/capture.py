import argparse
import libcamera
import mmap
import numpy as np
import os.path
import os
from select import select
import simplejpeg
import time
import threading
from inotify_simple import INotify, flags as IFlags

class YuvBuffer:
    def __init__(self, planes, size):
        map_length = max([plane.length + plane.offset for plane in planes])
        self.fd = planes[0].fd
        self.mapped = mmap.mmap(self.fd, map_length, mmap.MAP_SHARED | mmap.MADV_SEQUENTIAL | mmap.ACCESS_READ | mmap.PROT_WRITE)
        self.image = np.ndarray((map_length), dtype=np.uint8, buffer=self.mapped)
        [y, u, v] = [self.image[x.offset:x.offset+x.length] for x in planes]
        self.y = y.reshape(size.height, size.width)
        self.u = u.reshape(size.height // 2, size.width // 2)
        self.v = v.reshape(size.height // 2, size.width // 2)
        self.encoded = None

    def encode(self):
        if self.encoded is not None:
            del self.encoded
            self.encoded = None
        self.encoded = simplejpeg.encode_jpeg_yuv_planes(self.y, self.u, self.v, quality=85, fastdct=True)
        return self.encoded

    def __del__(self):
        del self.image
        self.mapped.close()
        del self.mapped
        if self.encoded is not None:
            del self.encoded

def _main(filename, working_filename, max_fps, overwrite_existing_temp_file, work_dir, max_width, max_height, list_resolutions, min_width, min_height, smallest_resolution):
    inotify = INotify()
    counter = 0
    assert os.path.isdir(work_dir), "Invalid workdir"
    working_path = os.path.join(work_dir, os.path.basename(working_filename))
    output_path = os.path.join(work_dir, os.path.basename(filename))
    frame_delay = None if max_fps is None else 1.0/max_fps

    cam_mgr = libcamera.CameraManager.singleton()
    now = lambda: time.clock_gettime(time.CLOCK_BOOTTIME)

    cam = cam_mgr.cameras[0]
    jpeg_encoders = []
    try:
        cam.acquire()
        conf = cam.generate_configuration([libcamera.StreamRole.StillCapture])
        stream_conf = conf.at(0)
        yuv420 = libcamera.PixelFormat('YUV420')
        stream_conf.pixel_format = [x for x in stream_conf.formats.pixel_formats if x == yuv420][0]
        stream_sizes = stream_conf.formats.sizes(stream_conf.pixel_format)
        matching_stream_sizes = [size for size in stream_sizes if
                                 (max_width is None or size.width <= max_width) and
                                 (max_height is None or size.height <= max_height) and
                                 (min_width is None or size.width >= min_width) and
                                 (min_height is None or size.height >= min_height)]
        if list_resolutions or not matching_stream_sizes:
            if not list_resolutions:
                print("No matching stream sizes found.")
            stream_sizes = ", ".join((f"{size.width}x{size.height}" for size in stream_sizes))
            print("Availailable sizes: {stream_sizes}")
            return
        stream_conf.size = matching_stream_sizes[0] if smallest_resolution else matching_stream_sizes[-1]
        stream_conf.buffer_count = 2
        assert conf.validate().value == 0
        cam.configure(conf)
        frame_microseconds = int((1.0/max_fps)*10**6)
        print(f"Setting frame limit to {frame_microseconds}")
        cam.controls[libcamera.controls.FrameDurationLimits] = (frame_microseconds, frame_microseconds)
        cam_controls = {libcamera.controls.FrameDurationLimits: (frame_microseconds, frame_microseconds)}
        select([cam_mgr.event_fd], [], [], 2.0)

        stream = stream_conf.stream
        allocator = libcamera.FrameBufferAllocator(cam)
        allocator.allocate(stream)
        requests = []
        buffers = list(allocator.buffers(stream))
        for buffer in buffers:
            request = cam.create_request(len(requests))
            request.add_buffer(stream, buffer)
            requests.append(request)
            jpeg_encoders.append(None)
        frame_timestamp = None
        cam_start = cam.start

        try:
            while True:
                if cam_start is not None:
                    cam_start(cam_controls)
                    cam_start = None
                    requests[0].reuse()
                    cam.queue_request(requests[0])

                request = cam_mgr.get_ready_requests()
                if request:
                    request = request[-1]
                else:
                    select([cam_mgr.event_fd], [], [])
                    request = cam_mgr.get_ready_requests()[-1]
                next_request = requests[request.cookie ^ 1]
                next_request.reuse()
                cam.queue_request(next_request)

                buffer = request.buffers[stream]
                planes_fd = buffer.planes[0].fd
                assert [x for x in buffer.planes if x.fd != planes_fd] == [], "Planes from different fd:s not supported"
                size = stream_conf.size
                jpeg_encoder = jpeg_encoders[request.cookie]
                if jpeg_encoder is not None and jpeg_encoder.fd != buffer.planes[0].fd:
                    del jpeg_encoder
                    jpeg_encoder = None
                if jpeg_encoder is None:
                    jpeg_encoder = jpeg_encoders[request.cookie] = YuvBuffer(buffer.planes, size)
                encoded = jpeg_encoder.encode()
                output = os.open(working_path, os.O_RDWR | os.O_CREAT, mode=0o755)
                try:
                    encoded_len = len(encoded)
                    os.truncate(output, encoded_len)
                    with mmap.mmap(output, encoded_len, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE) as mapped:
                        mapped.seek(0)
                        mapped.write(encoded)
                        mapped.flush()
                finally:
                    os.close(output)
                current_id = inotify.add_watch(working_path, IFlags.OPEN)
                os.rename(working_path, output_path)
                while True:
                    ready, _, _ = select([inotify, cam_mgr.event_fd] if cam_start is None else [inotify], [], [])
                    if inotify in ready:
                        inotify.rm_watch(current_id)
                        while inotify.read(timeout=0):
                            pass
                        break
                    elif cam_mgr.event_fd in ready:
                        req = cam_mgr.get_ready_requests()[-1]
                        cam.stop()
                        cam_start = cam.start
        finally:
            cam.stop()
    finally:
        for jpeg_encoder in [x for x in jpeg_encoders if x]:
            del jpeg_encoder
        cam.release()

if __name__ == "__main__":
    args = argparse.ArgumentParser(prog="libcamera-lazy-capture")
    args.add_argument('-f', '--filename', type=str, default='current.jpg')
    args.add_argument('--working-filename', type=str, default='saving.jpg')
    args.add_argument('--max-fps', type=float, default=30.0)
    args.add_argument('--max-width', type=int, default=None)
    args.add_argument('--max-height', type=int, default=None)
    args.add_argument('--min-width', type=int, default=None)
    args.add_argument('--min-height', type=int, default=None)
    args.add_argument('--smallest-resolution', action='store_true')
    args.add_argument('--list-resolutions', action='store_true')
    args.add_argument('--overwrite-existing-temp-file', action='store_true')
    args.add_argument('work_dir', type=str)
    args = args.parse_args()
    _main(**vars(args))

