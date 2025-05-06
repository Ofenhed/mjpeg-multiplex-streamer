This is a complete[^dumb] MJPEG server. The reason it was built is that my
previous MJPEG server could lag behind, sometimes quite a bit. This seemed to
happen when the server was busy, possibly because of multiple open streams. I
also took issue with the mjpg server using a lot of cpu power even when nobody
was looking at the stream.

What this implementation adds is a separation between the clients, and
automatic camera capture suspension when nobody is listening. The client
separation makes it so that you can have two clients with different bandwidths,
where one can receive the stream at 1 FPS and the other can receive the stream
at 5 FPS with both of them still receiving the latest possible image from the
camera.

## Usage
The server is built to be started by a systemd socket. This simplifies
permission handling, as systemd can sandbox the program, and it gives you the
option to easily [limit resource
usage](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)
of the MJPEG server.

The files defined in [systemd/](systemd/) is an example which assumes that the
files from [pysrc/](pysrc/) is installed in `/opt/mjpeg-multiplex-streamer/`,
with a venv installed in `/opt/mjpeg-multiplex-streamer/.venv`.

> [!note]
> I had some issues installing the [requirements.txt](pysrc/requirements.txt)
> on the Raspberry Pi, because of limited memory available and libcamera trying
> to compile in parallel. I solved this by using `python3-libcamera` from `apt`
> by setting `include-system-site-packages` to `true` in
> `/opt/mjpg-multiplex-streamer/.venv/pyvenv.cfg`.

The script [haproxy\_docker\_script.sh](docker/haproxy_docker_script.sh) is
intended for the octoprint docker container. It reduces the buffering of
haproxy (at the cost of increased CPU usage), which limits the delay of frames
on the MJPEG stream.

## Architecture
It's split into two simple parts, [capture.py](pysrc/capture.py) and
[server.py](pysrc/server.py).

### [capture.py](pysrc/capture.py):
Create a file (e.g. `/run/webcam/current.jpg`) and create an inotify listener
for whenever anyone accesses that file. Whenever that listener triggers,
atomically replace it with a new file (by simply renaming the new file to
`current.jpg`). Any client that had `current.jpg` open when the file was
replaced will remain access to the file, as it is still allocated as an
anonymous file until the last reader closes their file descriptor.

As long as anyone reads `current.jpg` before the next frame is generated, it
will keep generating frames in the configured intervals of the camera. If the
camera captures a frame without anyone reading `current.jpg`, the capture will
stop (without releasing the camera) and restart (without reconfiguring the
camera) whenever anyone reads `current.jpg`.

### [server.py](pysrc/server.py):
Check the HTTP request for whether to send a snapshot or a stream, then create
an inotify listener in the parent directory of a predetermined file (e.g.
`/run/webcam/current.jpg`). Any file detected is written to the client.

To make sure that frame latency is as small as possible, caching is disabled to
the highest degree possible. If this results in the transfer being to slow to
send all frames, then the server will simply always send the latest available
frame.

[^dumb]: Currently, the parsing of HTTP data is beyond trivial.
