"""S2-10: a Windows GPU box installs its NVIDIA driver itself.

Until 1.4.0 `create` handed over two PowerShell commands for a person to run at
an Administrator prompt, because Google documented no unattended method. It
does now. "Install GPU drivers" (https://cloud.google.com/compute/docs/gpus/
install-drivers-gpu, last updated 2026-10-26, read 2026-11-03), "Windows
(automated)":

    To automatically install GPU drivers on Windows machine first boot, set the
    sysprep-specialize-script-url metadata key for the new instance to
    https://github.com/GoogleCloudPlatform/compute-gpu-installation/raw/main/windows/install_gpu_driver.ps1 .

These tests hold `create` to that, `go` to waiting for it, and Linux to being
exactly as it was.
"""

from __future__ import annotations

from comfy_qa.config import Host
from comfy_qa.create import (
    CARDS, IMAGES, LINUX_DRIVER, NO_GPU, WINDOWS_DRIVER_URL, Blueprint, next_steps,
)

URL = ("https://github.com/GoogleCloudPlatform/compute-gpu-installation/raw/main/"
       "windows/install_gpu_driver.ps1")

WIN_L4 = Blueprint(name="comfy-win", image=IMAGES["windows"], card=CARDS["l4"])
WIN_NONE = Blueprint(name="qatest-win", image=IMAGES["windows"], card=NO_GPU)
LINUX_L4 = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["l4"])


def _keys(metadata: str) -> dict:
    return dict(pair.split("=", 1) for pair in metadata.split(","))


def test_a_windows_gpu_box_is_created_with_google_s_driver_script():
    assert WINDOWS_DRIVER_URL == URL
    keys = _keys(WIN_L4.metadata)
    assert keys == {"enable-windows-ssh": "TRUE", "sysprep-specialize-script-url": URL}


def test_a_windows_box_with_no_gpu_gets_no_driver():
    assert WIN_NONE.metadata == "enable-windows-ssh=TRUE"


def test_no_manual_powershell_step_is_printed_or_needed():
    told = "\n".join(next_steps(WIN_L4, "us-central1-a"))
    for manual in ("Invoke-WebRequest", "install_gpu_driver.ps1", "PowerShell",
                   "Administrator"):
        assert manual not in told, told
    assert "installs the NVIDIA driver" in told
    assert "comfy-qat go comfy-win" in told


def test_the_plan_says_the_driver_installs_itself():
    steps = "\n".join(WIN_L4.steps("us-central1-a"))
    assert "NOT installed" not in steps
    assert "sysprep-specialize" in steps and "NVIDIA driver" in steps


def test_the_linux_path_is_unchanged():
    assert LINUX_L4.metadata == f"startup-script={LINUX_DRIVER}"
    assert "reboot" in "\n".join(next_steps(LINUX_L4, "us-central1-a"))


def test_go_waits_for_the_driver_on_a_windows_gpu_box():
    from comfy_qa import lifecycle

    asked = []

    class Box:
        def ssh_output(self, instance, zone, project, remote):
            asked.append(remote)
            return "GPU 0: NVIDIA L4 (UUID: GPU-x)"

    win = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
               gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
               gce_project="p")
    lifecycle.wait_for_driver(Box(), win, lambda line: None)
    assert asked == ["nvidia-smi -L"], "a Windows GPU box is asked, as Linux is"


def test_go_does_not_wait_for_a_driver_on_a_windows_box_with_no_gpu():
    from comfy_qa import lifecycle

    class Box:
        def ssh_output(self, *args):
            raise AssertionError("asked a box with no GPU for its driver")

    win = Host(name="qatest-win", kind="gce", port=8190, os="Windows Server 2022",
               gpu="none", gce_instance="qatest-win", gce_zone="us-central1-a",
               gce_project="p")
    lifecycle.wait_for_driver(Box(), win, lambda line: None)


def test_a_windows_driver_that_never_comes_up_points_at_the_serial_port():
    """Google's script runs in sysprep's specialize pass, before anyone can SSH
    in, and its output goes to the serial console — so that is where the fix
    sends you, not to a Linux installer log."""
    from comfy_qa import lifecycle
    from comfy_qa.gcloud import GcloudError
    from comfy_qa.lifecycle import LifecycleError

    class Box:
        def ssh_output(self, *args):
            raise GcloudError("'nvidia-smi' is not recognized")

    win = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
               gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
               gce_project="p")
    clock = iter(range(0, 10**6, 100))
    try:
        lifecycle.wait_for_driver(Box(), win, lambda line: None, timeout=300,
                                  sleep=lambda s: None, now=lambda: next(clock))
    except LifecycleError as exc:
        assert "get-serial-port-output comfy-win" in exc.fix
        assert "cuda-installer" not in exc.fix
    else:
        raise AssertionError("a driver that never came up was waited out silently")
