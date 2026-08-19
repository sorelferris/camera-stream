import json

import zmq
from rich import print

context = zmq.Context()
stream = context.socket(zmq.SUB)
stream.setsockopt(zmq.RCVHWM, 1)
stream.setsockopt(zmq.LINGER, 0)
stream.setsockopt(zmq.SUBSCRIBE, b"base_camera/color")
stream.connect("tcp://192.168.5.24:5555")

try:
    while True:
        topic, header_bytes, payload = stream.recv_multipart()
        print(topic.decode("utf-8"))
        header = json.loads(header_bytes.decode("utf-8"))
        print(header)
        # Decode JPEG with cv2.imdecode(...) when header["codec"] == "jpeg".
finally:
    stream.close()
    context.term()
