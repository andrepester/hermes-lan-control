"""Copy as sitecustomize.py into a disposable native-test Python venv only.

The plugin's UDP sender is intercepted at Python's socket audit boundary.
Never install this guard into a production or shared interpreter.
"""
import sys

LAN_CONTROL_NATIVE_UDP_GUARD = True


def reject_udp_send(event, args):
    if event == "socket.sendto":
        raise RuntimeError("Native compatibility test forbids UDP sends")


sys.addaudithook(reject_udp_send)
