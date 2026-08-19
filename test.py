import json

import zmq

context = zmq.Context()
stream = context.socket(zmq.SUB)
stream.setsockopt(zmq.RCVHWM, 1)
stream.setsockopt(zmq.LINGER, 0)
stream.setsockopt(zmq.SUBSCRIBE, b"status/")
stream.connect("tcp://192.168.5.24:5555")

try:
    while True:
        topic, payload = stream.recv_multipart()
        message = json.loads(payload.decode("utf-8"))
        if topic == b"status/snapshot" and message.get("type") == "snapshot":
            for camera in message["cameras"]:
                print(camera["name"], camera["state"])
        elif topic.startswith(b"status/camera/"):
            print(topic.decode(), message["state"], message.get("error"))
finally:
    stream.close()
    context.term()
