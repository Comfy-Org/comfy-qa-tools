"""A cloud that costs nothing, and a ComfyUI that is really there.

The `up / open / down / go / move` path is the least-tested part of this tool for
one reason: exercising it for real starts a GPU instance, takes minutes and bills
by the second. So it was tested a step at a time, with everything around each
step replaced — which is exactly how the failures that matter get through, because
they live between the steps.

Two stand-ins let the whole path run in a test:

`FakeGcloud` implements the same methods as `Gcloud` and is scripted per test. It
keeps the box's state — a machine that has been started is RUNNING afterwards, a
machine that has been installed onto answers INSTALLED — so a flow that skips a
step fails here the way it would fail on a real box.

`FakeComfyUI` is a real HTTP server on a real ephemeral port. Nothing is patched
out of the readiness probe or of `stamp.fetch`: they open real sockets and parse
real responses. That is the point. Its `reset` mode — accept the connection, then
drop it — is what an IAP tunnel to a box with no ComfyUI on it actually does, and
it is not something a stubbed probe would ever have shown us.
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from comfy_qa.gcloud import GcloudError

# ---------------------------------------------------------------- the cloud


def _resolve(spec, *args):
    """A scripted behaviour: raise it, call it, or return it."""
    if isinstance(spec, BaseException):
        raise spec
    if callable(spec):
        return spec(*args)
    return spec


class FakeGcloud:
    """Everything this tool asks Google to do, without asking Google.

    Defaults describe the happy path: the box is RUNNING, has no ComfyUI on it,
    accepts commands, installs successfully and launches successfully. Each is
    overridable with a value, or with an exception to raise instead.
    """

    def __init__(
        self,
        *,
        statuses=("RUNNING",),
        installed: bool = False,
        start=None,
        stop=None,
        ssh_ready: bool | int | BaseException = True,
        install_exit: int = 0,
        install_works: bool = True,
        launch_exit: int | list[int] = 0,
        repair_exit: int = 0,
        # What the box says when asked whether ComfyUI could actually start.
        # READY is the happy path; TORCH_NO_CUDA is a real L4 box that pip had
        # quietly given a CPU-only torch.
        verify: str = "READY",
        # "<pid> <name>", as the box's own port check prints it. Empty means the
        # port is free, which is the ordinary case.
        port_holder: str = "",
        # Already open by default: a test about launching is not a test about
        # firewalls, and the rule is asked for on every launch.
        firewall=({"name": "comfy-qat-iap-comfyui",
                   "network": ".../networks/default"},),
        on_launch=None,
        describe=None,
        snapshot=None,
        create_disk=None,
        create_instance=None,
        disks=(),
        snapshots=(),
        instances=(),
    ) -> None:
        # `relocate.survey` lists disks, snapshots and instances before touching
        # anything — that read is what turns up the leftovers a half-finished
        # move left billing. Empty by default: a clean project.
        self.disks = list(disks)
        self.snapshots = list(snapshots)
        self.instances = list(instances)
        self.statuses = list(statuses)
        self.installed = installed
        self.start = start
        self.stop = stop
        self.ssh_ready = ssh_ready
        self.install_exit = install_exit
        self.install_works = install_works
        # A list means "this, then that" — a launch that fails, is repaired,
        # and succeeds needs two different answers from one fake.
        self.launch_exit = list(launch_exit) if isinstance(launch_exit, list) else launch_exit
        self.repairs = 0
        self.repair_exit = repair_exit
        self.verify = verify
        self.port_holder = port_holder
        self.firewall = [dict(rule) for rule in firewall]
        self.created_firewall = None
        self.on_launch = on_launch
        self.describe = describe or {}
        self.snapshot = snapshot
        self.create_disk = create_disk
        self.create_instance = create_instance

        # Whether a start actually succeeded — a start that raised leaves the box
        # off, and nothing is billing.
        self.running_now = bool(statuses) and statuses[0] == "RUNNING"
        self.calls: list[tuple] = []
        self.remote: list[str] = []
        self._ssh_attempts = 0

    # --- what the tool actually calls ------------------------------------

    def instance_status(self, name: str, zone: str, project: str) -> str:
        self.calls.append(("instance_status", name, zone, project))
        state = self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)
        return _resolve(state)

    def start_instance(self, name: str, zone: str, project: str) -> None:
        self.calls.append(("start_instance", name, zone, project))
        _resolve(self.start)
        self.running_now = True

    def stop_instance(self, name: str, zone: str, project: str) -> None:
        self.calls.append(("stop_instance", name, zone, project))
        _resolve(self.stop)

    def ssh_output(self, instance: str, zone: str, project: str, remote: str) -> str:
        self.calls.append(("ssh_output", instance, zone, project))
        self.remote.append(remote)
        if "echo ok" in remote:
            self._ssh_attempts += 1
            if isinstance(self.ssh_ready, BaseException):
                raise self.ssh_ready
            if self.ssh_ready is False:
                raise GcloudError("failed to connect to backend")
            if isinstance(self.ssh_ready, int) and not isinstance(self.ssh_ready, bool):
                if self._ssh_attempts < self.ssh_ready:
                    raise GcloudError("failed to connect to backend")
            return "ok"
        if "INSTALLED" in remote:  # the check script
            return "INSTALLED" if self.installed else "MISSING"
        if "TORCH_NO_CUDA" in remote:  # the verify script
            return self.verify
        if "NetFirewallRule" in remote or "ufw" in remote:
            return "ALREADY"
        if "NetTCPConnection" in remote or "sport = :" in remote:
            return self.port_holder or "PORT_FREE"
        return ""

    def ssh(self, instance: str, zone: str, project: str, remote: str,
            *, stream: bool = True) -> int:
        self.calls.append(("ssh", instance, zone, project))
        self.remote.append(remote)
        if "clone" in remote:  # the install script
            if self.install_exit == 0 and self.install_works:
                self.installed = True
            return _resolve(self.install_exit)
        if "pip install -r requirements.txt" in remote:  # the repair
            self.repairs += 1
            return self.repair_exit
        # the launch script
        if self.on_launch is not None:
            self.on_launch()
        if isinstance(self.launch_exit, list):
            return self.launch_exit.pop(0) if self.launch_exit else 0
        return _resolve(self.launch_exit)

    def firewall_rules(self, project: str) -> list[dict]:
        self.calls.append(("firewall_rules", project))
        return list(self.firewall)

    def create_firewall_rule(self, name: str, project: str, **kwargs) -> None:
        self.calls.append(("create_firewall_rule", name, project))
        self.firewall.append({"name": name, "network": ".../networks/default"})
        self.created_firewall = kwargs

    def list_instances(self, project: str) -> list[dict]:
        self.calls.append(("list_instances", project))
        return list(self.instances)

    def run(self, args, **kwargs):
        """The generic escape hatch `relocate` uses for the two list calls.

        Only reads are answered here. Anything else is a call this fake was never
        told to expect, and raising is the point — a test asserting `calls == []`
        under `--dry-run` is how the "a dry run must not start a GPU box" rule is
        held, and a fake that quietly returned None for everything would let that
        regress silently.
        """
        self.calls.append(("run", tuple(args)))
        joined = " ".join(str(part) for part in args)
        if "disks list" in joined:
            return list(self.disks)
        if "snapshots list" in joined:
            return list(self.snapshots)
        if "machine-types list" in joined:
            return [{"name": "g2-standard-8"}]
        if "disks create" in joined:
            # `relocate` builds this call itself so it can pass --type and keep
            # the new disk the same kind as the one it copies; a pd-balanced
            # original was silently becoming pd-standard. Recorded under the old
            # verb so the assertions about what a move does still read.
            self.calls.append(("create_disk_from_snapshot", args[3]))
            _resolve(self.create_disk)
            return []
        if "snapshots delete" in joined:
            self.calls.append(("delete_snapshot", args[3]))
            return []
        raise AssertionError(f"fake gcloud was not told to expect: {joined}")

    def describe_instance(self, name: str, zone: str, project: str) -> dict:
        self.calls.append(("describe_instance", name, zone, project))
        return _resolve(self.describe) or {}

    def delete_snapshot(self, snapshot: str, project: str) -> None:
        # A move deletes its own snapshot once the instance exists: the new disk
        # is a full copy and the original box is still there, so keeping it is a
        # third copy nobody reads and everybody pays for.
        self.calls.append(("delete_snapshot", snapshot, project))

    def snapshot_disk(self, disk: str, zone: str, project: str, snapshot: str) -> None:
        self.calls.append(("snapshot_disk", disk, zone, project, snapshot))
        _resolve(self.snapshot)

    def create_disk_from_snapshot(self, disk: str, zone: str, project: str,
                                  snapshot: str) -> None:
        self.calls.append(("create_disk_from_snapshot", disk, zone, project, snapshot))
        _resolve(self.create_disk)

    def create_instance_from_disk(self, name: str, zone: str, project: str, disk: str,
                                  machine_type: str, metadata: str | None = None,
                                  **network) -> None:
        # `network` carries external_ip / network / subnet — recorded, because a
        # moved box with no egress cannot install anything, and a test that
        # ignored these would not notice that regression.
        self.calls.append(("create_instance_from_disk", name, zone, project, disk))
        self.created_with = network
        _resolve(self.create_instance)

    # --- what a test asks it ---------------------------------------------

    def remote_commands_joined(self) -> str:
        return "\n".join(self.remote)

    def did(self, verb: str) -> bool:
        return any(call[0] == verb for call in self.calls)

    def count(self, verb: str) -> int:
        return sum(1 for call in self.calls if call[0] == verb)


# ------------------------------------------------------------- the ComfyUI

# A real /system_stats body, trimmed to the keys this tool reads.
SYSTEM_STATS = {
    "system": {
        "os": "nt",
        "comfyui_version": "0.3.44",
        "python_version": "3.12.13 (main, Aug 14 2025, 11:12:11) [MSC v.1929]",
        "pytorch_version": "2.8.0+cu128",
    },
    "devices": [
        {"name": "cuda:0 NVIDIA L4", "type": "cuda", "vram_total": 23609475072},
    ],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep pytest output clean
        pass

    def handle_one_request(self) -> None:
        if self.server.mode == "reset":
            # What an IAP tunnel to a box with nothing on 8188 does: the local
            # end accepts, then the connection dies. SO_LINGER 0 makes it an RST
            # rather than a polite close, which is what the real one sends.
            try:
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close()
            except OSError:
                pass
            self.close_connection = True
            return
        super().handle_one_request()

    def do_GET(self) -> None:
        mode = self.server.mode
        if mode == "garbage":
            body = b"<html>this is not ComfyUI</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif self.path.rstrip("/") != "/system_stats":
            self.send_response(404)
            self.end_headers()
            return
        else:
            body = json.dumps(self.server.payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeComfyUI:
    """A real HTTP server standing in for ComfyUI, on a real localhost port.

    `mode` is switched by the test as the flow proceeds — a box that has nothing
    on it until the install finishes is `reset` and then `serving`.
    """

    def __init__(self, mode: str = "serving", payload: dict | None = None) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.mode = mode
        self.server.payload = payload or SYSTEM_STATS
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        # A short poll interval only so shutdown is quick; nothing depends on it.
        self._thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self._thread.start()

    @property
    def mode(self) -> str:
        return self.server.mode

    @mode.setter
    def mode(self, value: str) -> None:
        self.server.mode = value

    def stop(self) -> None:
        """Stop answering at all — the port goes to "connection refused"."""
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


def free_port() -> int:
    """A port with nothing on it, for the "nothing is listening" case."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------ clocks


class Clock:
    """Time that moves when the code sleeps, so timeouts cost no real seconds.

    Every `sleep` also yields for a moment of real time, because the readiness
    watcher runs in another thread and has to get a turn.
    """

    def __init__(self, real_pause: float = 0.002) -> None:
        self.t = 0.0
        self.real_pause = real_pause
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def pause(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
        time.sleep(self.real_pause)


# ----------------------------------------------------------------- tunnels


def fake_tunnel_launcher(processes: list) -> callable:
    """A launcher that starts a real, harmless process that stands in for a tunnel.

    Not a made-up pid: the pid file, the liveness check and the SIGTERM in `down`
    all have to work against something real. It carries the tunnel's argv too, so
    the process reads as one to anybody looking; what the tool itself asks — "is
    this still the process I recorded" — is answered by the table in conftest.py,
    so no result here depends on this platform's `ps`.
    """

    def launch(cmd: list[str], log: Path) -> int:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", *cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        processes.append(process)
        return process.pid

    return launch


def hosts_toml(path: Path, *, remote_port: int, local_port: int = 8188,
               name: str = "comfy-win", os_name: str = "Windows Server 2022") -> Path:
    """A host list with one local machine and one cloud box."""
    path.write_text(
        "[hosts.local]\n"
        "kind = 'local'\n"
        f"port = {local_port}\n"
        "\n"
        f"[hosts.{name}]\n"
        "kind = 'gce'\n"
        f"os = '{os_name}'\n"
        "gpu = 'L4'\n"
        f"gce_instance = '{name}'\n"
        "gce_zone = 'us-central1-a'\n"
        "gce_project = 'comfy-qa'\n"
        f"port = {remote_port}\n",
        encoding="utf-8",
    )
    return path
