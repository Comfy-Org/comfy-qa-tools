"""Rewriting the host list without losing it.

Every other write to hosts.toml appends, which cannot lose anything. This one
rewrites, and the file is hand-maintained, carries comments, and names every
machine the user can reach — a truncated hosts.toml is worse than any move
failure, because after it no command works at all.

So these tests are about the failure, not the feature.
"""

from __future__ import annotations

import inspect
import itertools
import re
import tomllib

import pytest

from comfy_qa.hostfile import HostFileError, apply, rename_and_add, without

HOSTS = """\
# my machines — this comment must survive
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192

# the windows one
[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190
"""

ADDED = """
[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8192
"""


def renamed():
    return rename_and_add(HOSTS, name="comfy-linux",
                          renamed="comfy-linux-us-central1-c",
                          renamed_port=8193, added=ADDED)


def test_the_moved_box_keeps_its_name_and_its_port():
    parsed = tomllib.loads(renamed())["hosts"]
    assert parsed["comfy-linux"]["port"] == 8192
    assert parsed["comfy-linux"]["gce_zone"] == "us-central1-a"


def test_the_old_box_stays_reachable_under_a_name_that_says_where_it_is():
    """It exists in GCE and bills until deleted. Dropping it from the list would
    make it unstoppable by this tool, which is a money bug dressed as tidying."""
    parsed = tomllib.loads(renamed())["hosts"]
    old = parsed["comfy-linux-us-central1-c"]
    assert old["gce_zone"] == "us-central1-c"
    assert old["port"] == 8193, "the canonical port went to the new box"


def test_unrelated_hosts_and_comments_survive():
    out = renamed()
    assert "# my machines — this comment must survive" in out
    assert "# the windows one" in out
    parsed = tomllib.loads(out)["hosts"]
    assert parsed["comfy-win"]["port"] == 8190
    assert parsed["local"]["port"] == 8188


def test_renaming_a_host_that_is_not_there_is_refused():
    with pytest.raises(HostFileError, match="not in the host list"):
        rename_and_add(HOSTS, name="nope", renamed="nope-x",
                       renamed_port=8199, added=ADDED)


def test_a_block_with_no_port_line_is_refused_rather_than_silently_colliding():
    text = "[hosts.comfy-linux]\nkind = \"gce\"\n"
    with pytest.raises(HostFileError, match="no port line"):
        rename_and_add(text, name="comfy-linux", renamed="comfy-linux-z",
                       renamed_port=8193, added=ADDED)


def test_a_crlf_file_comes_back_uniformly_crlf_from_a_rename():
    """A CRLF file in, a CRLF file out — every line, including the rewritten one.

    Three sites had to be right for this, and they are one defect wearing three
    hats. The header match consumes the trailing `\r`, so the one line a rename
    rewrites was the one LF line in the file. `without`'s separator was
    hard-coded `\n\n`. And `discover.to_toml` hard-codes `\n` for the block
    appended here, so on the real path — the one `move` takes — nine more LF
    lines arrived from the caller.

    All three parse, so `apply` refuses none of them, and each is a whole-file
    diff in git for a change to one host.

    `added` is deliberately LF here, because that is what `to_toml` hands over.
    """
    text = HOSTS.replace("\n", "\r\n")
    assert lone_line_feeds(text) == 0, "the input is uniformly CRLF"

    out = rename_and_add(text, name="comfy-linux", renamed="comfy-linux-z",
                         renamed_port=8195, added=ADDED)

    assert lone_line_feeds(out) == 0
    assert "[hosts.comfy-linux-z]\r\n" in out
    assert "\r\nport         = 8195\r\n" in out, "the port line too"
    assert "[hosts.comfy-linux]\r\n" in out, "and the block appended by the caller"
    assert tomllib.loads(out)["hosts"]["comfy-linux-z"]["port"] == 8195


def test_a_crlf_file_comes_back_uniformly_crlf_from_a_removal():
    """The same rule in the sibling function, whose separator was hard-coded."""
    text = HOSTS.replace("\n", "\r\n")
    out = without(text, "comfy-linux")

    assert lone_line_feeds(out) == 0
    assert set(tomllib.loads(out)["hosts"]) == {"local", "comfy-win"}
    assert "# the windows one" in out, "the comment still survives"


def test_an_lf_file_is_never_given_carriage_returns():
    """The other direction, which is the ordinary case and must not move."""
    out = rename_and_add(HOSTS, name="comfy-linux", renamed="comfy-linux-z",
                         renamed_port=8195, added=ADDED)
    assert "\r" not in out
    assert "\r" not in without(HOSTS, "comfy-linux")


def test_a_file_that_already_mixes_line_endings_is_not_guessed_at():
    """Mixed endings are not something this can repair, and picking a side
    rewrites lines nobody asked it to touch. So it is treated as LF and the
    rewrite adds nothing new rather than normalising the whole file."""
    text = HOSTS.replace("\n", "\r\n").replace(
        '[hosts.local]\r\n', '[hosts.local]\n')
    assert lone_line_feeds(text) == 1, "the input is genuinely mixed"

    out = without(text, "comfy-win")
    assert set(tomllib.loads(out)["hosts"]) == {"local", "comfy-linux"}


def lone_line_feeds(text: str) -> int:
    """LF characters not preceded by CR — the lines that are not CRLF."""
    return sum(1 for index, char in enumerate(text)
               if char == "\n" and (index == 0 or text[index - 1] != "\r"))


# --- apply(): nothing reaches the file unless it is right --------------------

def test_a_result_that_would_not_parse_never_lands(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    with pytest.raises(HostFileError, match="would not parse"):
        apply(path, "[hosts.broken\n", expect={"x"})
    assert path.read_text(encoding="utf-8") == HOSTS, "the original was touched"


def test_a_result_missing_a_host_never_lands(tmp_path):
    """The failure this check exists for: a transform that quietly drops a box."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    with pytest.raises(HostFileError, match="missing comfy-win"):
        apply(path, "[hosts.local]\nkind = \"local\"\nport = 8188\n",
              expect={"local", "comfy-win"})
    assert path.read_text(encoding="utf-8") == HOSTS


def test_a_good_rewrite_lands_and_keeps_a_copy_of_what_was_there(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    apply(path, renamed(),
          expect={"local", "comfy-win", "comfy-linux", "comfy-linux-us-central1-c"})

    assert tomllib.loads(path.read_text(encoding="utf-8"))["hosts"]["comfy-linux"][
        "gce_zone"] == "us-central1-a"
    backup = path.with_name("hosts.toml.bak")
    assert backup.exists() and backup.read_text(encoding="utf-8") == HOSTS


def test_a_second_rewrite_does_not_destroy_the_first_one_s_copy(tmp_path):
    """The sequence that actually happens: move, notice something is wrong, move
    again. One backup deep, the second write destroys the copy of the state you
    wanted back — and this file has no other copy anywhere on the machine."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")

    once = renamed()
    apply(path, once, expect={"local", "comfy-win", "comfy-linux",
                              "comfy-linux-us-central1-c"})
    twice = rename_and_add(once, name="comfy-win", renamed="comfy-win-us-central1-a",
                           renamed_port=8196, added=ADDED_HOST)
    apply(path, twice, expect={"local", "comfy-win-us-central1-a", "comfy-linux",
                               "comfy-linux-us-central1-c", "added"})

    assert path.with_name("hosts.toml.bak").read_text(encoding="utf-8") == once
    archived = list((tmp_path / "backups").glob("hosts.toml.*.bak"))
    assert [item.read_text(encoding="utf-8") for item in archived] == [HOSTS], (
        "the original is still recoverable after a second write"
    )


def test_an_unchanged_file_does_not_churn_the_generations(tmp_path):
    """Rotating on every call would push the state you want off the end with
    copies of itself. Only a backup whose content actually differs rotates."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    for _ in range(3):
        apply(path, HOSTS, expect={"local", "comfy-win", "comfy-linux"})

    assert path.with_name("hosts.toml.bak").read_text(encoding="utf-8") == HOSTS
    assert not (tmp_path / "backups").exists(), "nothing was archived"


def test_only_the_newest_few_superseded_copies_are_kept(tmp_path):
    """Capped, and pruned oldest-first AFTER the new copy is on the disk — a
    prune that ran first and then failed to write would leave fewer copies than
    it started with."""
    import time

    from comfy_qa.hostfile import BACKUP_GENERATIONS

    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    for port in range(9000, 9000 + BACKUP_GENERATIONS + 3):
        apply(path, HOSTS.replace("port = 8188", f"port = {port}"),
              expect={"local", "comfy-win", "comfy-linux"})
        time.sleep(0.01)   # the archive is named to the second; keep them ordered

    archived = list((tmp_path / "backups").glob("hosts.toml.*.bak"))
    contents = [item.read_text(encoding="utf-8") for item in archived]
    # The literal 5, not BACKUP_GENERATIONS: a count checked against the very
    # constant that produced it moves with it, so it cannot fail. The cap is
    # part of the promise, not an implementation detail, and this is the line
    # that goes red when somebody changes it.
    assert len(archived) == 5, (
        f"the cap is 5 archived generations; {len(archived)} survived "
        f"{BACKUP_GENERATIONS + 3} writes"
    )

    # Oldest-first: the very first state is gone, the recent ones are not. A
    # copy is archived one write AFTER it stops being live — it spends that
    # write as `.bak` — so the newest archived state is two writes behind.
    assert HOSTS not in contents, "the oldest was pruned"
    newest = max(archived, key=lambda item: item.stat().st_mtime)
    assert f"port = {9000 + BACKUP_GENERATIONS}" in newest.read_text(encoding="utf-8")


def test_a_backup_that_cannot_be_written_refuses_the_rewrite(tmp_path):
    """The strongest form of the rule: a move that cannot be undone does not
    happen. The host list is hand-maintained and has no other copy anywhere."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    original_mode = tmp_path.stat().st_mode
    tmp_path.chmod(0o500)               # r-x: readable, nothing new lands
    try:
        with pytest.raises(HostFileError, match="could not be copied"):
            apply(path, renamed(), expect={"local", "comfy-win", "comfy-linux",
                                           "comfy-linux-us-central1-c"})
    finally:
        tmp_path.chmod(original_mode)

    assert path.read_text(encoding="utf-8") == HOSTS, "it was rewritten anyway"


def test_a_backup_that_does_not_read_back_stops_the_write(tmp_path):
    """A backup nobody has read back is a belief, not a copy — and the next step
    rewrites the only file there is. Declining is recoverable; writing over the
    last copy of a hand-maintained file is not."""
    from comfy_qa import hostfile as hostfile_module

    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    honest = hostfile_module._write_durably

    def lossy(target, content, *, like=None):
        # The shape a failing disk gives you: the write returns, the bytes are
        # not what you passed.
        if target.name.endswith(".bak"):
            content = b""
        honest(target, content, like=like)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(hostfile_module, "_write_durably", lossy)
    try:
        with pytest.raises(HostFileError, match="not recoverable"):
            apply(path, renamed(), expect={"local", "comfy-win", "comfy-linux",
                                           "comfy-linux-us-central1-c"})
    finally:
        monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == HOSTS, "the file was rewritten anyway"


def test_the_bytes_are_on_the_disk_before_the_rename_and_the_rename_after(tmp_path):
    """`os.replace` is atomic in ORDERING, which is not the same as durable.

    Without the fsync on the file, a crash can leave the rename visible over
    content that never reached the disk. Without the one on the DIRECTORY, the
    rename itself can be lost while the data survives — the file reverts and
    `.bak` says the write happened.
    """
    import os as os_module

    from comfy_qa import hostfile as hostfile_module

    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    order: list[str] = []
    real_fsync, real_replace = os_module.fsync, os_module.replace

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(hostfile_module.os, "fsync",
                        lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(hostfile_module.os, "replace",
                        lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    try:
        apply(path, renamed(), expect={"local", "comfy-win", "comfy-linux",
                                       "comfy-linux-us-central1-c"})
    finally:
        monkeypatch.undo()

    # The exact sequence, because a looser assertion cannot see the middle one
    # go missing: "an fsync immediately before the replace" is satisfied by the
    # BACKUP's fsync, so removing the new file's own left this test green.
    #
    #   fsync   the backup's bytes
    #   fsync   the new file's bytes
    #   replace the rename
    #   fsync   the directory entry
    assert order == ["fsync", "fsync", "replace", "fsync"], order


def test_the_backup_does_not_widen_the_permissions_of_what_it_copies(tmp_path):
    """A file created at the default umask is more permissive than the one it is
    a copy of. Nothing in a host list is a secret; a file that quietly widens its
    own permissions on every write is the kind of thing that is true of something
    else later."""
    import stat as stat_module

    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    path.chmod(0o600)

    apply(path, renamed(), expect={"local", "comfy-win", "comfy-linux",
                                   "comfy-linux-us-central1-c"})

    for name in ("hosts.toml", "hosts.toml.bak"):
        mode = stat_module.S_IMODE(path.with_name(name).stat().st_mode)
        assert mode == 0o600, f"{name} came out {mode:o}"


def test_no_temp_file_is_left_beside_the_host_list(tmp_path):
    """A stray .tmp next to hosts.toml is the kind of thing somebody later has to
    make a decision about."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    apply(path, renamed(),
          expect={"local", "comfy-win", "comfy-linux", "comfy-linux-us-central1-c"})
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


# --- the file that parses, counts right, and still bricks the tool -----------
#
# Found by adversarial testing, and it is the worst thing this module can do:
# names all correct, TOML valid, and `config.load` then refuses it — after which
# no comfy-qat command works until somebody hand-edits the file. Worse, register()
# runs at the REGISTER step, so the box is already moved, running and billing, and
# `down` can no longer reach it.

MOVED_ONCE = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-linux-us-central1-c]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8194

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8192
"""

COMING_HOME = """
[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
"""


def test_moving_a_box_back_where_it_came_from_is_refused_not_written(tmp_path):
    """A stockout pushes the box out of us-central1-c; capacity returns; you move
    it home. The retired entry still names instance `comfy-linux` in zone c, and
    so does the one coming home. Two names, one machine — which the loader
    rejects, correctly, because reading results from the wrong box is the thing
    this tool exists to prevent."""
    path = tmp_path / "hosts.toml"
    path.write_text(MOVED_ONCE, encoding="utf-8")

    text = rename_and_add(MOVED_ONCE, name="comfy-linux",
                          renamed="comfy-linux-us-central1-a",
                          renamed_port=8195, added=COMING_HOME)

    # It parses, and every name is exactly what was expected. That is why the
    # old check waved it through.
    assert tomllib.loads(text)
    expect = {"local", "comfy-linux-us-central1-c",
              "comfy-linux-us-central1-a", "comfy-linux"}
    assert set(tomllib.loads(text)["hosts"]) == expect

    with pytest.raises(HostFileError, match="would not load"):
        apply(path, text, expect=expect)
    assert path.read_text(encoding="utf-8") == MOVED_ONCE, "it was written anyway"
    assert not path.with_name("hosts.toml.bak").exists(), (
        "a refused write must not clobber the backup"
    )


def test_what_the_loader_refuses_this_refuses_to_write(tmp_path):
    """The rule, stated once: validation here is the real loader, not a proxy."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    same_machine = HOSTS + """
[hosts.a-different-name]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8199
"""
    with pytest.raises(HostFileError, match="would not load"):
        apply(path, same_machine,
              expect={"local", "comfy-linux", "comfy-win", "a-different-name"})
    assert path.read_text(encoding="utf-8") == HOSTS


# --- removing a host must not take the next one's comments ------------------
#
# `without` ran from the block header to the next `[`, so everything between the
# end of the deleted block and that bracket went too — which in a hand-maintained
# file is exactly where the NEXT host's comments live. Every guard passed: the
# file parsed, the host names matched `expect` exactly, config.parse accepted it.
# The name check cannot see it, because no name is lost. `delete` ended in "and it
# is out of your host list" — unqualified success, having destroyed a line that
# said DO NOT DELETE.

ANNOTATED = """\
[hosts.comfy-win]
kind         = "gce"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190

# comfy-linux holds the 70B checkpoint and the PM-630 fixtures.
# DO NOT DELETE. Ali, 2026-08-14.
[hosts.comfy-linux]
kind         = "gce"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
"""


def test_removing_a_host_keeps_the_next_one_s_comments():
    out = without(ANNOTATED, "comfy-win")

    assert "DO NOT DELETE" in out, "a warning the user wrote was destroyed"
    assert "70B checkpoint" in out
    assert "comfy-win" not in tomllib.loads(out)["hosts"]
    assert "comfy-linux" in tomllib.loads(out)["hosts"]


def test_removing_the_last_host_keeps_the_ones_above_it():
    out = without(ANNOTATED, "comfy-linux")

    assert "comfy-linux" not in tomllib.loads(out)["hosts"]
    assert tomllib.loads(out)["hosts"]["comfy-win"]["port"] == 8190
    assert "DO NOT DELETE" not in out, (
        "that comment belonged to the block being removed"
    )


def test_removing_a_host_with_no_blank_line_after_it_still_works():
    """Not every file is spaced out; the block simply ends at the next header."""
    tight = ("[hosts.a]\nkind = \"local\"\nport = 8188\n"
             "[hosts.b]\nkind = \"local\"\nport = 8189\n")
    out = without(tight, "a")
    assert set(tomllib.loads(out)["hosts"]) == {"b"}


def test_a_comment_directly_above_the_removed_block_goes_with_it():
    """It describes the host being deleted, so keeping it would strand a note
    about a machine that no longer exists."""
    src = ("[hosts.a]\nkind = \"local\"\nport = 8188\n\n"
           "# b is the windows one\n[hosts.b]\nkind = \"local\"\nport = 8189\n")
    assert "# b is the windows one" not in without(src, "b")


# --- the mirror, and CRLF -----------------------------------------------------
#
# The first fix stopped `without` taking the NEXT host's comments. It did not stop
# it taking the PREVIOUS host's, and that is the shape that hits a real file: the
# header pattern used `\s`, which matches newlines, so the match began on the blank
# line above the header and swallowed the very terminator the comment walk depends
# on. Three agents reproduced it independently, and on the real hosts.toml it
# destroyed all eight lines of the commented-out example `init` writes.

MIRROR = """\
[hosts.comfy-win]
kind = "gce"
port = 8190
# comfy-win is the PM-630 box. Keep the port, Linear links to it.

[hosts.comfy-linux]
kind = "gce"
port = 8191
"""


def test_deleting_a_host_keeps_the_previous_one_s_trailing_note():
    out = without(MIRROR, "comfy-linux")
    assert "PM-630 box" in out, "a note belonging to a host we did not touch"
    assert set(tomllib.loads(out)["hosts"]) == {"comfy-win"}


def test_the_same_holds_with_crlf_line_endings():
    """`[ \\t]*$` still fails on CRLF, because `$` matches before the \\n and the
    line ends in \\r. Getting that wrong reinstates the original defect in full."""
    out = without(MIRROR.replace("\n", "\r\n"), "comfy-linux")
    assert "PM-630 box" in out
    assert set(tomllib.loads(out)["hosts"]) == {"comfy-win"}


def test_the_real_host_lists_worked_example_survives_a_delete():
    """`init` writes a commented-out [hosts.comfy-linux] block into every file as
    a worked example. It sits directly above a real host, so it is exactly what
    the mirror destroys."""
    src = ("# a cloud host may never use 8188; that is the local ComfyUI's\n\n"
           "[hosts.local]\nkind = \"local\"\nport = 8188\n\n"
           "# [hosts.comfy-linux]\n# kind         = \"gce\"\n# port         = 8190\n\n"
           "[hosts.comfy-win]\nkind = \"gce\"\nport = 8191\n")
    out = without(src, "comfy-win")

    assert out.count("#") == src.count("#"), "a commented example was destroyed"
    assert "may never use 8188" in out
    assert set(tomllib.loads(out)["hosts"]) == {"local"}


def test_deleting_the_first_host_keeps_the_file_s_own_preamble():
    """The comment walk ran off the top of the file: with only comments above the
    header there is no blank line to stop it, so the whole preamble went.

    A run at offset 0 is either the file's preamble or the first host's own note,
    and nothing can tell them apart from the text — the obvious heuristic passes
    six cases and destroys a legitimate seventh. So this takes the asymmetry the
    rest of the tool takes about money and applies it to data: for something that
    cannot be undone, be wrong in the direction that leaves something behind.
    """
    src = ("# my machines - hand maintained, mind the comments\n"
           "# ports are allocated in order; do not reuse 8190\n"
           "[hosts.comfy-win]\nkind = \"gce\"\nport = 8190\n\n"
           "[hosts.local]\nkind = \"local\"\nport = 8188\n")
    out = without(src, "comfy-win")

    assert "hand maintained" in out, "the file's preamble was destroyed"
    assert "do not reuse 8190" in out
    assert set(tomllib.loads(out)["hosts"]) == {"local"}


def test_a_note_below_the_top_still_goes_with_its_own_host():
    """The rule only applies at offset 0. Anywhere else, a comment directly above
    a block describes that block and goes with it."""
    src = ("[hosts.local]\nkind = \"local\"\nport = 8188\n\n"
           "# comfy-win is the PM-630 box\n"
           "[hosts.comfy-win]\nkind = \"gce\"\nport = 8190\n")
    assert "PM-630" not in without(src, "comfy-win")


# --- the fixtures were kinder than a real file ------------------------------
#
# Every test above uses a host list written for this file. A move against the
# REAL hosts.toml on this machine failed while all of them passed, because the
# port pattern's trailing `\s*` ate the newline and welded the port line to the
# next section header:
#
#     port         = 8195[hosts.comfy-linux-a]
#
# `apply` then refused the write, correctly, and `move` failed. The suite was
# green throughout. So this fixture is deliberately shaped like a file somebody
# maintains rather than one written to pass.

LIVED_IN = """\
# comfy-qat host list.
#
# Rules the tool enforces:
#   - every host needs its own port
#   - a cloud host may never use 8188; that is the local ComfyUI's

[hosts.local]
kind = "local"
port = 8188

# [hosts.comfy-linux]
# kind         = "gce"
# port         = 8190

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
[hosts.comfy-linux-a]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux-a"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8193
"""


def test_a_move_against_a_lived_in_file_produces_valid_toml():
    out = rename_and_add(LIVED_IN, name="comfy-linux",
                         renamed="comfy-linux-us-central1-c",
                         renamed_port=8195,
                         added="\n[hosts.comfy-linux]\nkind = \"gce\"\n"
                               "gce_instance = \"comfy-linux\"\n"
                               "gce_zone = \"us-central1-a\"\n"
                               "gce_project = \"proj\"\nport = 8192\n")

    hosts = tomllib.loads(out)["hosts"]
    assert hosts["comfy-linux"]["gce_zone"] == "us-central1-a"
    assert hosts["comfy-linux"]["port"] == 8192
    assert hosts["comfy-linux-us-central1-c"]["port"] == 8195
    assert hosts["comfy-linux-a"]["port"] == 8193, "the next host was welded on"
    assert out.count("#") == LIVED_IN.count("#")


def test_removing_from_a_lived_in_file_keeps_everything_else():
    out = without(LIVED_IN, "comfy-linux-a")
    assert set(tomllib.loads(out)["hosts"]) == {"local", "comfy-linux"}
    assert out.count("#") == LIVED_IN.count("#")


# --- every shape, rather than one fixture per defect --------------------------
#
# `without` has now been fixed six times, and the sixth was found the way the
# first five should have been. Every test above is a hand-written fixture pinning
# the one shape that broke — so each fix guarded its own case and left the next
# unseen shape open. Five defects in a row, all in one function, all "the fixtures
# differ from a real file in exactly the way that hides it".
#
# So this stops hand-writing shapes. Nine binary axes, chosen because each one
# has broken this function at least once, or is the shape of a defect the earlier
# five could not generate: a file preamble, a comment above each host, a blank
# line between blocks, CRLF endings, a commented-out worked example like the one
# `init` writes, that example sitting in the GAP between two hosts rather than at
# the top, a note trailing a host's body rather than heading it, an INDENTED
# table header, and a comment on the END of the header and port lines rather than
# on a line of its own. 512 files, three victims each — 1,536 cases.
#
# This header said SEVEN axes and 128 files while `repeat=9` below generated 512,
# so it described less than a third of what actually runs. The two it left out
# are the two added most recently, and both already carry their own note further
# down: `indented` is D10, and mutation says it earns its place — reinstate that
# defect without it and every generated case passes. A header that undercounts
# the matrix is how an axis comes to be deleted as surplus.
#
# It asserts only what the module already promises: remove a host, and what is
# left is valid TOML holding exactly the other hosts, with their own notes intact.
#
# These do NOT replace the hand-written tests above. Proved, not assumed: restore
# D75's root cause (`\s` in `without`'s header pattern) and the generated cases
# fire ZERO — the named tests are the only thing that catches it.

SHAPES = list(itertools.product([False, True], repeat=9))
VICTIMS = ("alpha", "beta", "gamma")

# A note on a host's BODY, below its port line, rather than above its header.
# The `own_comment` axis only ever puts comments directly above a header, and a
# trailing note is a different shape for the comment walk to get wrong.
TRAILING = "# {name} is the PM-630 box. Linear links to this port."


# A comment on the END of a significant line, rather than on a line of its own.
# Both patterns used to stop at the value, so a host annotated this way was
# unmovable ("has no port line") and, with the note on its header, invisible to
# both move and delete ("is not in the host list"). The file's own preamble
# invites exactly this annotation.
INLINE_HEADER = "# {name} is pinned"
INLINE_PORT = "# {name}'s port, do not reuse"


def _lived_in(preamble, own_comment, blank_between, example, crlf,
              example_in_gap, trailing_note, indented=False, inline_comment=False):
    # TOML allows a table header to be indented, this module matches one on
    # purpose (`^[ \t]*\[hosts`), and D10 was the defect of not writing that
    # indentation back. Without this axis the matrix cannot generate the shape:
    # measured by mutation, reinstating D10 left every generated case passing.
    pad = "  " if indented else ""
    lines: list[str] = []
    if preamble:
        lines += ["# this file is hand maintained", "# mind the comments", ""]
    if example:
        lines += ["# [hosts.disabled]", "# port = 9999", ""]
    for index, name in enumerate(VICTIMS):
        if own_comment:
            lines.append(f"{pad}# {name} is the {name} box. DO NOT DELETE.")
        header = f"{pad}[hosts.{name}]"
        port = f"{pad}port = {8100 + index}"
        if inline_comment:
            header += "  " + INLINE_HEADER.format(name=name)
            port += "  " + INLINE_PORT.format(name=name)
        lines += [header, f'{pad}kind = "local"', port]
        if trailing_note:
            lines.append(pad + TRAILING.format(name=name))
        if blank_between and index < len(VICTIMS) - 1:
            lines.append("")
        # The worked example in the GAP between two hosts, not only at the top.
        # This is the shape `init` + `discover` produce: STARTER ends with the
        # commented-out example and `to_toml` begins with a newline, so on every
        # populated host list the example is one blank line from the next header.
        if example_in_gap and index == 0:
            lines += ["# [hosts.spare]", "# port = 9998"] + ([""] if blank_between else [])
    text = "\n".join(lines) + "\n"
    return text.replace("\n", "\r\n") if crlf else text


# --- which axis is which, looked up rather than counted on fingers ----------
#
# A `shape` is a tuple of booleans in `_lived_in`'s PARAMETER ORDER, and the
# filters over `SHAPES` index into it by position. `shape[5]` is not
# self-describing and, worse, it is not stable: this axis list has already grown
# from seven to nine, and an axis inserted anywhere but the end re-points every
# index after it. A filter written against the old numbering keeps running,
# against a different axis, and passes.
#
# So the positions are looked up BY NAME. Rename or drop an axis and `.index()`
# raises at import time — a collection error, which is impossible to miss —
# instead of the filter quietly coming to mean something else.
AXES = tuple(inspect.signature(_lived_in).parameters)
EXAMPLE_IN_GAP = AXES.index("example_in_gap")
BLANK_BETWEEN = AXES.index("blank_between")


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("victim", VICTIMS)
def test_removing_any_host_from_any_shaped_file_keeps_the_others(shape, victim):
    text = _lived_in(*shape)
    out = without(text, victim)

    hosts = tomllib.loads(out)["hosts"]
    assert set(hosts) == set(VICTIMS) - {victim}, (
        f"shape={shape} victim={victim}\n{out}"
    )

    if shape[1]:  # every host carried its own note
        for other in set(VICTIMS) - {victim}:
            assert f"# {other} is the {other} box" in out, (
                f"a note belonging to {other} was destroyed\n"
                f"shape={shape} victim={victim}\n{out}"
            )

    # Gated on `blank_between`, and the gate is load-bearing rather than
    # defensive. With NO blank line a trailing note touches the previous host's
    # body AND the next host's header, and the module deliberately gives a
    # contiguous comment run to the header that follows it — so ungated this
    # asserts a preference, not a promise. Ungated, the two new axes report 112
    # failures, most of them the module behaving as designed. Gated, 32, and
    # every one of those is real.
    if shape[8]:  # every host's header and port line carried an inline note
        for other in set(VICTIMS) - {victim}:
            assert INLINE_HEADER.format(name=other) in out, (
                f"{other}'s inline header note was destroyed\n"
                f"shape={shape} victim={victim}\n{out}")
            assert INLINE_PORT.format(name=other) in out, (
                f"{other}'s inline port note was destroyed\n"
                f"shape={shape} victim={victim}\n{out}")

    if shape[6] and shape[2]:  # a trailing note on a body, blocks separated
        for other in set(VICTIMS) - {victim}:
            assert TRAILING.format(name=other) in out, (
                f"{other}'s trailing note was destroyed\n"
                f"shape={shape} victim={victim}\n{out}"
            )

    # A CRLF file comes back uniformly CRLF here too. This function's separator
    # was hard-coded `\n\n` until today, and without this assertion the matrix
    # cannot see it go back: measured, that mutation passed 832 of 832.
    if shape[4]:
        assert lone_line_feeds(out) == 0, (
            f"a CRLF file came back with LF lines\nshape={shape} victim={victim}\n{out!r}")
    else:
        assert "\r" not in out, f"shape={shape} victim={victim}\n{out!r}"


# The gap-example assertion is its own test because it used to FAIL, and only
# for the host the example sits under: `without` ran the removal to the next `[`,
# and the walk back from there stopped at the blank line below the example, so a
# commented-out block in the gap went with the host above it. That was D93.
#
# It was recorded here as a STRICT xfail rather than fixed on the spot, and the
# strictness is what closed it: fixing D93 turned 128 xfails into XPASS and the
# suite went red, which is how a defect record is supposed to end. A non-strict
# xfail would have gone quietly green and stayed in the file as decoration.

# --- the same shapes, through the other function ------------------------------
#
# `without` had all of this and `rename_and_add` had none of it, which is not a
# defensible split: measured by mutation, reinstating D9 or D10 left the
# generated cases at 448 passed — the matrix could not see either, because both
# live here. The two functions are now covered by the same shapes.
#
# And this is the more expensive half. `without` runs after a box is already
# destroyed; `rename_and_add` is the write a MOVE makes, after the new instance
# exists and is billing. A rewrite that loses the block, or gives two hosts one
# port, strands a machine the tool can no longer name — which is the failure
# `move` was written to stop, arriving by a different road.

ADDED_HOST = "\n[hosts.added]\nkind = \"local\"\nport = 8200\n"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("victim", VICTIMS)
def test_renaming_any_host_in_any_shaped_file_keeps_the_others(shape, victim):
    text = _lived_in(*shape)
    crlf = shape[4]
    retired = f"{victim}-us-central1-c"

    # LF, always: `discover.to_toml` hard-codes it and cannot know what file the
    # block is going into. Handing this test a CRLF block instead would make the
    # whole caller-side half of the line-ending fix untestable — measured, that
    # mutation passed 832 of 832 before this line was written this way.
    out = rename_and_add(text, name=victim, renamed=retired,
                         renamed_port=8195, added=ADDED_HOST)
    why = f"shape={shape} victim={victim}\n{out!r}"

    hosts = tomllib.loads(out)["hosts"]
    assert set(hosts) == (set(VICTIMS) - {victim}) | {retired, "added"}, why

    # The renamed block took the new port, and nothing else moved. Two hosts on
    # one port is a config the loader refuses, and it lands at the REGISTER step
    # of a move — after the box is built and billing.
    assert hosts[retired]["port"] == 8195, why

    # The renamed header keeps its own indentation. TOML does not care, so the
    # parse above cannot see this go wrong — measured, reinstating D10 passed
    # every generated case until this assertion existed.
    pad = "  " if shape[7] else ""
    ending = "\r\n" if crlf else "\n"
    # The whole rewritten header line, including any inline note it carried:
    # both halves of what the match consumed have to come back, and in order.
    rewritten = f"{pad}[hosts.{retired}]"
    if shape[8]:
        rewritten += "  " + INLINE_HEADER.format(name=victim)
    assert rewritten + ending in out, why
    if not pad:
        assert f" [hosts.{retired}]" not in out, why
    for index, name in enumerate(VICTIMS):
        if name != victim:
            assert hosts[name]["port"] == 8100 + index, why
    assert len({host["port"] for host in hosts.values()}) == len(hosts), why

    # A rename rewrites one header and one port line. It is not a delete, so
    # EVERY comment survives — no gate here, unlike `without`.
    if shape[1]:
        for name in VICTIMS:
            assert f"# {name} is the {name} box" in out, why
    if shape[6]:
        for name in VICTIMS:
            assert TRAILING.format(name=name) in out, why
    if shape[8]:
        # The renamed host's OWN notes survive the rewrite — this is the pair of
        # lines the rewrite touches, so it is the only place they can be lost.
        assert INLINE_HEADER.format(name=victim) in out, why
        assert INLINE_PORT.format(name=victim) in out, why
        for name in VICTIMS:
            assert INLINE_PORT.format(name=name) in out, why
    if shape[5]:
        assert "# [hosts.spare]" in out, why
    if shape[3]:
        assert "# [hosts.disabled]" in out, why
    if shape[0]:
        assert "# this file is hand maintained" in out, why

    # A CRLF file comes back uniformly CRLF, the caller's LF block included.
    if crlf:
        assert lone_line_feeds(out) == 0, why
    else:
        assert "\r" not in out, why


# The shapes that actually put a worked example in a gap: the example has to be
# there (`example_in_gap`) AND there has to be a gap for it (`blank_between`).
GAP_SHAPES = [shape for shape in SHAPES
              if shape[EXAMPLE_IN_GAP] and shape[BLANK_BETWEEN]]


def test_the_gap_shape_filter_still_selects_the_shapes_it_is_named_for():
    """The floor under GAP_SHAPES, which is COMPUTED and then parametrised over.

    A parametrize over an empty list is ZERO TESTS AND GREEN. If this filter ever
    stops matching — an axis renamed, reordered, or added without widening the
    product — the test below does not go red, it DISAPPEARS, and D93 comes back
    with nothing watching for it. Nothing else in this file would notice: the
    other two parametrizes run over `SHAPES` and would still report their 1,536.

    So the selection is pinned by size in both directions, and then checked
    against the file it actually generates — because a size that happens to come
    out right while pointing at the wrong axis is the failure this is for.
    """
    assert len(SHAPES) == 2 ** len(AXES), (
        f"`SHAPES` enumerates {len(SHAPES)} combinations but `_lived_in` takes "
        f"{len(AXES)} axes ({', '.join(AXES)}). The `repeat=` above is a literal: "
        f"an axis added to `_lived_in` without widening it is never exercised — "
        f"it silently takes its default on every one of the {len(SHAPES)} cases — "
        f"and the matrix reports exactly the green it always did."
    )
    assert EXAMPLE_IN_GAP != BLANK_BETWEEN, "two axes collapsed onto one position"
    assert GAP_SHAPES, (
        "GAP_SHAPES is EMPTY. `test_a_worked_example_in_the_gap_survives_a_"
        "removal` is parametrised over it, so it now collects ZERO cases: the "
        "file passes without checking D93 even once. That is not a smaller "
        "failure than a red test, it is a larger one."
    )
    assert len(GAP_SHAPES) == len(SHAPES) // 4, (
        f"GAP_SHAPES selects {len(GAP_SHAPES)} of {len(SHAPES)} shapes. Two "
        f"independent booleans out of {len(AXES)} select exactly a quarter, "
        f"{len(SHAPES) // 4}. Any other number means the filter is no longer "
        f"reading the two axes it names — including the case where it has "
        f"become effectively `if True` and is quietly the whole matrix again."
    )

    # And it means this in the FILE, not merely in the tuple. If either index
    # re-pointed, these shapes would still number a quarter of the matrix while
    # generating files with no example in any gap — which is a filter that has
    # stopped selecting anything real, passing every size check above.
    for shape in GAP_SHAPES:
        text = _lived_in(*shape)
        assert "# [hosts.spare]" in text, f"no worked example at all\nshape={shape}"
        assert (text.index("[hosts.alpha]")
                < text.index("# [hosts.spare]")
                < text.index("[hosts.beta]")), (
            f"the worked example is not in the gap between two hosts, so this "
            f"shape cannot exercise D93 at all\nshape={shape}\n{text}"
        )


@pytest.mark.parametrize("shape", GAP_SHAPES)
@pytest.mark.parametrize("victim", VICTIMS)
def test_a_worked_example_in_the_gap_survives_a_removal(shape, victim):
    out = without(_lived_in(*shape), victim)
    assert "# [hosts.spare]" in out, (
        f"the worked example in the gap was destroyed\n"
        f"shape={shape} victim={victim}\n{out}"
    )


# --- the ending survives the READ, not just the transform ----------------------
#
# Everything above hands `without` and `rename_and_add` a CRLF string built in
# the test. Nothing above reads one off disk the way the product does, and that
# is the entire defect these cover.
#
# `Path.read_text()` opens with `newline=None` — universal newline mode — so
# every `\r\n` becomes `\n` before this module sees it. `_line_ending` then
# answers `"\n"`, `_in` rewrites nothing, and `apply` writes the whole file back
# as LF. Both production callers did exactly that: `remove.py` for `delete` and
# `host.py` for `move`. 512 shapes x 3 victims x 2 functions stayed green the
# whole time, because their input was a string no caller ever produced.
#
# Proportion, so nobody softens these later: the defect the matrix above was
# built to stop put ONE lone LF into a CRLF file. This put one in every line, on
# the command that cannot be undone and on the one nobody has run on hardware.
#
# So these assert on the FILE, and they get there through `hostfile.read`.

CRLF_HOSTS = HOSTS.replace("\n", "\r\n")


def _crlf_file(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_bytes(CRLF_HOSTS.encode("utf-8"))
    assert lone_line_feeds(path.read_bytes().decode()) == 0, "the fixture is CRLF"
    return path


def test_read_keeps_the_carriage_returns_that_read_text_eats(tmp_path):
    """The one-line difference the whole finding rests on."""
    from comfy_qa.hostfile import read

    path = _crlf_file(tmp_path)
    assert "\r\n" not in path.read_text(encoding="utf-8"), (
        "read_text is universal-newline mode — if this ever fails, Python "
        "changed and these tests are the wrong shape")
    assert "\r\n" in read(path), "hostfile.read must not translate"


def test_a_crlf_file_read_from_disk_comes_back_crlf_from_a_removal(tmp_path):
    """`delete`'s path, end to end, through the file rather than a string."""
    from comfy_qa.hostfile import read

    path = _crlf_file(tmp_path)
    out = without(read(path), "comfy-win")

    assert lone_line_feeds(out) == 0, (
        "every line of a CRLF host list was rewritten as LF by a delete")
    assert set(tomllib.loads(out)["hosts"]) == {"local", "comfy-linux"}


def test_a_crlf_file_read_from_disk_comes_back_crlf_from_a_rename(tmp_path):
    """`move`'s path. Never run on hardware, so this is the only thing holding
    it — and R5c in the criteria pack has never been ticked."""
    from comfy_qa.hostfile import read

    path = _crlf_file(tmp_path)
    out = rename_and_add(
        read(path), name="comfy-win", renamed="comfy-win-us-central1-a",
        renamed_port=8195, added='\n[hosts.comfy-win]\nkind = "gce"\nport = 8190\n')

    assert lone_line_feeds(out) == 0, (
        "every line of a CRLF host list was rewritten as LF by a move")
    assert "[hosts.comfy-win-us-central1-a]\r\n" in out
    assert "[hosts.comfy-win]\r\n" in out, "the appended block took the ending too"


def test_the_whole_delete_write_leaves_the_file_crlf(tmp_path):
    """Through `apply`, so the bytes that land on disk are what is asserted.

    `apply` writes what it is given. If the ending was lost at the read, this is
    where it becomes permanent, and the file a person opens next is the evidence.
    """
    from comfy_qa.hostfile import read

    path = _crlf_file(tmp_path)
    apply(path, without(read(path), "comfy-win"), expect={"local", "comfy-linux"})

    assert lone_line_feeds(path.read_bytes().decode("utf-8")) == 0


# --- the same shapes again, delivered the way the product delivers them --------
#
# The matrix above varies the TEXT and holds the DELIVERY constant, and the
# delivery was the bug. Every one of its 512 shapes is handed to the function as
# a string built in the test; no caller ever built one that way. Both real
# callers went through `Path.read_text()`, which normalises `\r\n` to `\n`, so
# every CRLF path inside this module was unreachable in production for its whole
# life — green, exhaustive, and about a shape the product could not produce.
#
# Switching the callers to `hostfile.read` makes real CRLF text reach `without`
# and `rename_and_add` FOR THE FIRST TIME. So the matrix has to be re-run through
# the file, not just re-read: a green run before that change proves nothing about
# after it, because the matrix is the thing that was testing the unreachable
# shape.
#
# Only the CRLF half of the shapes — the LF half is already the delivered case.

CRLF_SHAPES = [shape for shape in SHAPES if shape[4]]


def _on_disk(tmp_path, shape):
    """A shaped host list written as BYTES and read back through `hostfile.read`."""
    from comfy_qa.hostfile import read

    text = _lived_in(*shape)
    path = tmp_path / "hosts.toml"
    path.write_bytes(text.encode("utf-8"))
    delivered = read(path)
    assert delivered == text, "the read changed the file before anything touched it"
    return delivered


def test_the_crlf_half_of_the_matrix_is_actually_half_of_it():
    """A filter that matched nothing would make both matrices below vacuous."""
    assert len(CRLF_SHAPES) == len(SHAPES) // 2 == 256
    assert all(lone_line_feeds(_lived_in(*shape)) == 0 for shape in CRLF_SHAPES)


@pytest.mark.parametrize("shape", CRLF_SHAPES)
@pytest.mark.parametrize("victim", VICTIMS)
def test_removing_a_host_from_a_crlf_file_read_from_disk(shape, victim, tmp_path):
    """`delete`, every CRLF shape, delivered through the file."""
    out = without(_on_disk(tmp_path, shape), victim)

    assert set(tomllib.loads(out)["hosts"]) == set(VICTIMS) - {victim}, (
        f"shape={shape} victim={victim}\n{out}")
    assert lone_line_feeds(out) == 0, (
        f"a CRLF file read from disk came back with LF lines\n"
        f"shape={shape} victim={victim}\n{out!r}")


@pytest.mark.parametrize("shape", CRLF_SHAPES)
@pytest.mark.parametrize("victim", VICTIMS)
def test_renaming_a_host_in_a_crlf_file_read_from_disk(shape, victim, tmp_path):
    """`move`, every CRLF shape, delivered through the file.

    The more expensive half, and the one with no hardware behind it at all:
    phase R has never been run by anyone, so this is the only thing holding it.
    """
    added = '\n[hosts.newcomer]\nkind = "local"\nport = 8199\n'
    out = rename_and_add(_on_disk(tmp_path, shape), name=victim,
                         renamed=f"{victim}-us-central1-c", renamed_port=8196,
                         added=added)

    hosts = tomllib.loads(out)["hosts"]
    assert set(hosts) == (set(VICTIMS) - {victim}) | {
        f"{victim}-us-central1-c", "newcomer"}, f"shape={shape} victim={victim}\n{out}"
    assert lone_line_feeds(out) == 0, (
        f"a CRLF file read from disk came back with LF lines\n"
        f"shape={shape} victim={victim}\n{out!r}")
    # Written to match what `_TRAILING` actually captures, which is the point of
    # the assertion: indentation before the header, and spaces or a TOML comment
    # after it, all of which the rewrite has to put back along with the `\r`.
    # Two earlier drafts of this line failed on 384 shapes each and both times it
    # was the assertion, not the module — first stripping double spaces, then
    # requiring `\r\n` flush against the `]`.
    assert re.search(
        rf"^[ \t]*\[hosts\.{re.escape(victim)}-us-central1-c\][ \t]*(?:#[^\r\n]*)?\r\n",
        out, re.MULTILINE), f"the renamed header lost its ending\n{out!r}"
