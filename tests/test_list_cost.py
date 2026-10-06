"""`comfy-qat list` and `list --live` — the cost side, through the real CLI.

`list` is the command people read before they close the laptop, so what it says
about money has to be something it read. Two rules are held here.

**Plain `list` asks Google nothing.** It shows RESERVED from the host list and
says that is where it came from. AGE and DISK are not shown at all, because
nothing offline knows them and a column of dashes reads as an answer.

**`list --live` shows three cost columns it checked** — RESERVED, AGE, DISK —
from exactly two reads per project, and says `unchecked` or `unknown` in the
cell where a read did not come back. It never exits non-zero for a failed read.

Columns are asserted BOTH ways: present where they belong and absent where they
do not. "AGE is not in the plain table" passes on an empty page, so every
absence below sits beside the presence of something else on the same run.

Nothing here reaches Google, and nothing reads the real host list: every run
passes `--config`. Payloads are FIXTURES in the SDK schema's shape, not live
recordings. The local machine's "is ComfyUI answering" probe is replaced, since
whether something is listening on this laptop's 8188 is not this file's subject.
"""

from __future__ import annotations

import shlex
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa import host as host_module
from comfy_qa import inventory
from comfy_qa.cli import app
from comfy_qa.gcloud import Gcloud as RealGcloud
from comfy_qa.gcloud import GcloudError

PROJECT = "stately-timing-504610-p1"
ZONE = "us-central1-a"
URL = "https://www.googleapis.com/compute/v1/projects"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

HOSTS = f"""\
[hosts.local]
kind = "local"
port = 8188

[hosts.held]
kind            = "gce"
os              = "Ubuntu 22.04"
gpu             = "T4"
gce_instance    = "held"
gce_zone        = "{ZONE}"
gce_project     = "{PROJECT}"
gce_reservation = "held-rsv"
port            = 8191

[hosts.plain]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "plain"
gce_zone     = "{ZONE}"
gce_project  = "{PROJECT}"
port         = 8192

[hosts.cpu]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "none"
gce_instance = "cpu"
gce_zone     = "{ZONE}"
gce_project  = "{PROJECT}"
port         = 8193
"""

LOCAL_ONLY = '[hosts.local]\nkind = "local"\nport = 8188\n'


def instance(name, *, status="RUNNING", created="2026-10-02T12:00:00.000+00:00",
             disks=("200",), bound=None):
    row = {"name": name, "zone": f"{URL}/{PROJECT}/zones/{ZONE}", "status": status,
           "creationTimestamp": created,
           "disks": [{"diskSizeGb": size} for size in disks]}
    if bound:
        row["reservationAffinity"] = {
            "consumeReservationType": "SPECIFIC_RESERVATION",
            "key": "compute.googleapis.com/reservation-name", "values": [bound]}
    return row


def reserved(name, *, box=""):
    return {
        "name": name, "zone": f"{URL}/{PROJECT}/zones/{ZONE}", "status": "READY",
        "specificReservationRequired": True,
        "description": f"comfy-qat: held for {box}" if box else "made in the console",
        "specificReservation": {"count": "1", "instanceProperties": {
            "machineType": "n1-standard-8",
            "guestAccelerators": [{"acceleratorType": "nvidia-tesla-t4",
                                   "acceleratorCount": 1}]}},
    }


INSTANCES = [instance("held", status="TERMINATED", bound="held-rsv"),
             instance("plain", created="2026-10-05T07:00:00.000+00:00",
                      disks=("200", "50")),
             instance("cpu", created="2026-10-05T11:40:00.000+00:00")]
RESERVATIONS = [reserved("held-rsv", box="held")]


class Cloud:
    """The two reads `list --live` makes. Anything else is the failure."""

    def __init__(self, instances=None, reservations=None):
        self._instances = INSTANCES if instances is None else instances
        self._reservations = RESERVATIONS if reservations is None else reservations
        self.calls: list[str] = []

    def list_instances(self, project):
        self.calls.append(f"list_instances {project}")
        if isinstance(self._instances, BaseException):
            raise self._instances
        return list(self._instances)

    def list_reservations(self, project):
        self.calls.append(f"list_reservations {project}")
        if isinstance(self._reservations, BaseException):
            raise self._reservations
        return list(self._reservations)

    def __getattr__(self, item):  # pragma: no cover - the guard, not the path
        if item.startswith("_"):
            raise AttributeError(item)
        raise AssertionError(f"list asked the fake for {item!r}")


def _never(*_args, **_kwargs):
    raise AssertionError("plain `list` built a Gcloud, so it was about to call Google")


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def invoke(*args, declared=HOSTS, cloud=None):
        path = tmp_path / "hosts.toml"
        path.write_text(declared, encoding="utf-8")
        live = "--live" in args
        cloud = cloud if cloud is not None else Cloud()
        monkeypatch.setattr(gcloud_module, "Gcloud",
                            (lambda *a, **k: cloud) if live else _never)
        monkeypatch.setattr(host_module, "_answering", lambda host: False)
        monkeypatch.setattr(inventory, "_now", lambda: NOW)
        result = CliRunner().invoke(app, ["list", *args, "--config", str(path)])
        result.cloud = cloud        # type: ignore[attr-defined]
        return result

    return invoke


def table(output: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    """The header's columns, and each row as `{column: cell}`.

    Cells are cut at the header's own column starts, so a cell with a space in
    it (`200 GB`, `yes, unchecked`) is read whole.
    """
    lines = output.splitlines()
    head = next(line for line in lines if line.startswith("NAME"))
    names = head.split()
    starts = [head.index(name) for name in names]
    rows: dict[str, dict[str, str]] = {}
    for line in lines[lines.index(head) + 1:]:
        if not line.strip():
            break
        cells = [line[start:end].strip()
                 for start, end in zip(starts, starts[1:] + [None])]
        rows[cells[0]] = dict(zip(names, cells))
    return names, rows


# --- plain `list`: offline ---------------------------------------------------


def test_plain_list_makes_no_cloud_call_at_all(cli):
    """The fixture replaces `Gcloud` with something that raises on construction,
    so reaching for Google here is a traceback and a non-zero exit."""
    result = cli()
    assert result.exit_code == 0, result.output
    assert "held" in result.stdout


def test_plain_list_has_the_reserved_column_and_not_age_or_disk(cli):
    names, _rows = table(cli().stdout)
    assert names == ["NAME", "KIND", "OS", "GPU", "URL", "STATE", "RESERVED"]


def test_plain_list_shows_reserved_from_the_host_list(cli):
    _names, rows = table(cli().stdout)
    assert rows["held"]["RESERVED"] == "yes"
    assert rows["plain"]["RESERVED"] == "no"
    assert rows["cpu"]["RESERVED"] == "no"
    assert rows["local"]["RESERVED"] == "-"


def test_plain_list_shows_none_for_a_box_with_no_gpu(cli):
    _names, rows = table(cli().stdout)
    assert rows["cpu"]["GPU"] == "none"
    assert rows["plain"]["GPU"] == "L4"


def test_plain_list_says_where_reserved_came_from_and_what_a_reserved_box_costs(cli):
    """The literal sentences, typed here rather than imported."""
    out = cli().stdout
    assert ("RESERVED is what your host list says. A reserved box bills every "
            "hour, running or stopped.") in out
    assert "checks the reservations" in out
    assert "age and disk" in out


def test_a_host_list_with_no_cloud_box_is_not_told_about_reservations(cli):
    """Nothing in it can be reserved, and a footnote about a column of dashes
    is a sentence about nothing."""
    out = cli(declared=LOCAL_ONLY).stdout
    names, rows = table(out)
    assert "RESERVED" in names and rows["local"]["RESERVED"] == "-"
    assert "RESERVED is what your host list says" not in out
    assert "--live asks" in out, "the STATE footnote is still there"


def test_no_line_under_the_plain_table_runs_past_the_prose_width(cli):
    """A footnote is prose. 96 columns is `say.PROSE_WIDTH`."""
    out = cli().stdout
    footnotes = out[out.index("STATE is only"):].splitlines()
    assert footnotes, "there is a footnote to measure"
    assert max(len(line) for line in footnotes) <= 96, footnotes


# --- `list --live`: two reads, three checked columns -------------------------


def test_live_makes_exactly_two_calls_for_one_project(cli):
    """Three cloud boxes, one project: one `instances list`, one
    `reservations list`. Not one per box, and not a third read for anything."""
    result = cli("--live")
    assert result.exit_code == 0, result.output
    assert sorted(result.cloud.calls) == [f"list_instances {PROJECT}",
                                          f"list_reservations {PROJECT}"]


def test_live_with_no_cloud_boxes_asks_google_nothing(cli):
    result = cli("--live", declared=LOCAL_ONLY)
    assert result.exit_code == 0, result.output
    assert result.cloud.calls == []


def test_live_has_all_three_cost_columns_in_order(cli):
    names, _rows = table(cli("--live").stdout)
    assert names == ["NAME", "KIND", "OS", "GPU", "URL", "STATE", "RESERVED",
                     "AGE", "DISK"]


def test_live_shows_age_and_disk_from_the_instance_record(cli):
    _names, rows = table(cli("--live").stdout)
    assert (rows["held"]["AGE"], rows["held"]["DISK"]) == ("3d", "200 GB")
    assert (rows["plain"]["AGE"], rows["plain"]["DISK"]) == ("5h", "250 GB")
    assert (rows["cpu"]["AGE"], rows["cpu"]["DISK"]) == ("<1h", "200 GB")
    assert (rows["local"]["AGE"], rows["local"]["DISK"]) == ("-", "-")


def test_live_shows_reserved_as_checked(cli):
    _names, rows = table(cli("--live").stdout)
    assert rows["held"]["RESERVED"] == "yes"
    assert rows["plain"]["RESERVED"] == "no"
    assert rows["local"]["RESERVED"] == "-"


def test_live_still_says_what_each_box_is_doing(cli):
    _names, rows = table(cli("--live").stdout)
    assert rows["held"]["STATE"] == "stopped"
    assert rows["plain"]["STATE"] == "running"
    assert rows["local"]["STATE"] == "not serving"


def test_live_says_a_reserved_box_bills_even_though_it_is_stopped(cli):
    """`held` is stopped AND reserved on this run — the row a tired reader takes
    for free. The sentence is typed here, not imported."""
    out = cli("--live").stdout
    assert "A reserved box bills every hour, running or stopped" in out


def test_live_does_not_say_reserved_came_from_the_host_list(cli):
    """Under --live it was checked, and the plain table's caveat would be false."""
    out = cli("--live").stdout
    assert "RESERVED" in out
    assert "RESERVED is what your host list says" not in out


def test_live_without_a_reserved_box_says_nothing_about_reserved_bills(cli):
    out = cli("--live", cloud=Cloud(instances=[instance("plain")],
                                    reservations=[])).stdout
    _names, rows = table(out)
    assert rows["plain"]["RESERVED"] == "no"
    assert "A reserved box bills" not in out


def test_the_widest_live_row_stays_inside_120_columns_with_room(cli):
    """A table is not prose, but it is still pasted into Slack. The bound is the
    one the quota table is held to, and the fixture's widest row has margin
    under it, so this is a bound and not a boundary."""
    out = cli("--live", cloud=Cloud(
        instances=[instance("held", bound="made-by-hand"), instance("plain"),
                   instance("cpu")],
        reservations=[reserved("made-by-hand")])).stdout
    _names, rows = table(out)
    assert rows["held"]["RESERVED"] == "yes (not in host list)", "the widest cell"
    widest = max(len(line) for line in out.splitlines() if line.startswith(
        ("NAME", "held", "plain", "cpu", "local")))
    assert 60 < widest <= 115, widest


# --- each failure row --------------------------------------------------------


def test_a_declared_reservation_the_project_does_not_have_is_missing(cli):
    _names, rows = table(cli("--live", cloud=Cloud(reservations=[])).stdout)
    assert rows["held"]["RESERVED"] == "missing"
    assert rows["plain"]["RESERVED"] == "no"


def test_a_box_not_on_the_project_has_dashes_for_age_and_disk(cli):
    result = cli("--live", cloud=Cloud(instances=[instance("plain")], reservations=[]))
    _names, rows = table(result.stdout)
    assert rows["cpu"]["STATE"] == "not on the project"
    assert (rows["cpu"]["AGE"], rows["cpu"]["DISK"]) == ("-", "-")
    assert (rows["plain"]["AGE"], rows["plain"]["DISK"]) == ("3d", "200 GB")


def test_a_failed_instances_read_is_unknown_and_unchecked_and_still_exits_zero(cli):
    result = cli("--live", cloud=Cloud(instances=GcloudError("permission denied")))
    assert result.exit_code == 0, result.output
    _names, rows = table(result.stdout)
    for name in ("held", "plain", "cpu"):
        assert rows[name]["STATE"] == "unknown"
        assert (rows[name]["AGE"], rows[name]["DISK"]) == ("unknown", "unknown")
    assert rows["held"]["RESERVED"] == "yes, unchecked"
    assert rows["plain"]["RESERVED"] == "no, unchecked"


def test_a_failed_reservations_read_warns_and_says_unchecked_and_exits_zero(cli):
    result = cli("--live", cloud=Cloud(
        reservations=GcloudError("the reservations API is disabled")))
    assert result.exit_code == 0, result.output
    _names, rows = table(result.stdout)
    assert rows["held"]["RESERVED"] == "yes, unchecked"
    assert rows["plain"]["RESERVED"] == "no, unchecked"
    # The read that worked still filled its columns.
    assert (rows["plain"]["AGE"], rows["plain"]["DISK"]) == ("5h", "250 GB")
    # `say.warn` wraps prose at 96 columns, so the sentence is read unwrapped.
    assert (f"warning: could not ask Google about reservations on {PROJECT} "
            f"(the reservations API is disabled)") in " ".join(result.stderr.split())
    assert "warning" not in result.stdout, "a warning is the story, not the answer"


def test_a_good_run_warns_about_nothing(cli):
    result = cli("--live")
    assert "warning" not in result.output
    assert "unchecked" not in result.output and "unknown" not in result.output
    assert "yes" in result.stdout, "and it did print the table"


# --- reservations with no box ------------------------------------------------


def test_a_reservation_with_no_box_is_named_with_its_delete_command(cli):
    """The block from the design, typed out, on stdout: it is part of the answer
    to "what am I paying for"."""
    result = cli("--live", cloud=Cloud(
        reservations=[*RESERVATIONS, reserved("qatest-rsv", box="qatest")]))
    assert (f"1 reservation on {PROJECT} has no box, and is billing:\n"
            f"  qatest-rsv in {ZONE} (T4)\n"
            f"  gcloud compute reservations delete qatest-rsv --zone={ZONE} "
            f"--project={PROJECT}\n") in result.stdout


def test_the_orphan_delete_command_is_googles_own_and_parses_whole(cli):
    """It cannot be pasted into this CLI — it is gcloud's — so it is held to
    what E1's layer sends for the same release: same verb, same three values,
    one unwrapped line a shell reads as eight words."""
    result = cli("--live", cloud=Cloud(
        reservations=[reserved("qatest-rsv", box="qatest")]))
    line = next(text.strip() for text in result.stdout.splitlines()
                if "reservations delete" in text)
    words = shlex.split(line)
    assert words[:5] == ["gcloud", "compute", "reservations", "delete", "qatest-rsv"]
    assert sorted(words[5:]) == [f"--project={PROJECT}", f"--zone={ZONE}"]

    sent: list[list[str]] = []
    real = RealGcloud(runner=lambda args, mode: sent.append(args) or "")
    real.delete_reservation("qatest-rsv", ZONE, PROJECT)
    assert ["gcloud", *[arg for arg in sent[0] if arg != "--quiet"]] == words


def test_a_reservation_with_its_box_on_it_gets_no_orphan_block(cli):
    result = cli("--live")
    assert "has no box" not in result.output
    assert "reservations delete" not in result.output
    assert "held" in result.stdout


def test_no_orphan_block_when_the_boxes_could_not_be_read(cli):
    """Without the instances nobody knows which reservation has a box, and a
    delete command printed on that basis is a command against live capacity."""
    result = cli("--live", cloud=Cloud(instances=GcloudError("denied")))
    assert "has no box, and is billing" not in result.output
    assert "reservations delete" not in result.output
    assert "unknown" in result.stdout


def test_and_it_says_that_it_could_not_work_out_which_reservations_have_no_box(cli):
    """Withholding the orphan block is right. Saying nothing is not: a table
    with no block under it reads as a project where every reservation has its
    box. One warning per project, on stderr, with the command that lists them."""
    result = cli("--live", cloud=Cloud(
        instances=GcloudError("denied"),
        reservations=[*RESERVATIONS, reserved("qatest-rsv", box="qatest")]))

    assert result.exit_code == 0, result.output
    said = " ".join(result.stderr.split())
    assert (f"warning: could not ask Google about the boxes on {PROJECT}, so which "
            f"of its 2 reservations has no box on it was not worked out") in said
    assert f"gcloud compute reservations list --project={PROJECT}" in result.stderr
    assert "warning" not in result.stdout


def test_a_failed_instances_read_with_nothing_reserved_warns_of_nothing(cli):
    """The control: there is nothing that could have been an orphan."""
    result = cli("--live", cloud=Cloud(instances=GcloudError("denied"),
                                       reservations=[]))

    assert "was not worked out" not in result.output
    assert "unknown" in result.stdout
