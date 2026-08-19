from camera_stream_client import StreamClient

with StreamClient("tcp://192.168.5.24:5555") as client:
    camera = client.subscribe("base_camera/color")
    while True:
        frame = camera.read(timeout=1)
        image = frame.image
        print(f"Received frame from {camera.name} with shape {image.shape}")
