from overleaf_sjtu.client import SocketIO09Client


def test_socketio09_splits_single_packet() -> None:
    socket = SocketIO09Client.__new__(SocketIO09Client)

    assert socket._packets("1::") == ["1::"]


def test_socketio09_splits_framed_packets() -> None:
    socket = SocketIO09Client.__new__(SocketIO09Client)
    payload = "\ufffd3\ufffd1::\ufffd5\ufffd8::"

    assert socket._packets(payload) == ["1::", "8::"]
