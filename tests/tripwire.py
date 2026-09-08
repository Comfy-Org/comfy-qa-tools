"""Import FIRST in any in-process probe. Nothing may reach the outside world.

WHY THIS EXISTS, written down because I am the one who needed it.

Driving lifecycle's install ticker I ran `comfy-qat go` in-process against a
fake `Gcloud`. That fake covered nothing: `tunnel.py` DOES NOT GO THROUGH THE
`Gcloud` CLASS — it builds its own argv and spawns the binary — so the run
executed a real `gcloud compute ssh` and appended ~20 lines to the real
~/.config/comfy-qa-tools/tunnels/comfy-win.log. It died at local credential
refresh without reaching Google, but it should never have started.

The guard already existed. The suite's `cli` fixture monkeypatches
`tunnel_module.TUNNEL_DIR` to tmp_path on EVERY test, which is exactly this.
An ad-hoc probe does not get it for free, and nothing in the code path says so.

    import tripwire; tripwire.arm()      # before importing comfy_qa

Loopback is allowed: the ComfyUI port check is a real local connection and
forbidding it only teaches you to switch the tripwire off.
"""
import socket as _socket
import subprocess as _subprocess

LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
_real_connect = _socket.create_connection


def arm(*, allow_loopback: bool = True) -> None:
    def no_spawn(*a, **k):
        raise AssertionError(f"PROCESS SPAWN ATTEMPTED IN A PROBE: {a[:1]}")

    def guarded_connect(address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if allow_loopback and str(host) in LOOPBACK:
            return _real_connect(address, *a, **k)
        raise AssertionError(f"NON-LOOPBACK SOCKET ATTEMPTED IN A PROBE: {address}")

    _subprocess.Popen = no_spawn
    _subprocess.run = no_spawn
    _subprocess.call = no_spawn
    _subprocess.check_output = no_spawn
    _socket.create_connection = guarded_connect


def redirect_tunnels(tmp) -> None:
    """TUNNEL_DIR defaults to the REAL config directory. Always redirect it."""
    from comfy_qa import tunnel
    tunnel.TUNNEL_DIR = tmp
