"""Rewriting the host list without losing it.

Every other write to hosts.toml appends, which cannot lose anything. This one
rewrites, and the file is hand-maintained, carries comments, and names every
machine the user can reach — a truncated hosts.toml is worse than any move
failure, because after it no command works at all.

So these tests are about the failure, not the feature.
"""

from __future__ import annotations

import itertools
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
# So this stops hand-writing shapes. Seven binary axes, chosen because each one
# has broken this function at least once, or is the shape of a defect the earlier
# five could not generate: a file preamble, a comment above each host, a blank
# line between blocks, CRLF endings, a commented-out worked example like the one
# `init` writes, that example sitting in the GAP between two hosts rather than at
# the top, and a note trailing a host's body rather than heading it. 128 files,
# three victims each.
#
# It asserts only what the module already promises: remove a host, and what is
# left is valid TOML holding exactly the other hosts, with their own notes intact.
#
# These do NOT replace the hand-written tests above. Proved, not assumed: restore
# D75's root cause (`\s` in `without`'s header pattern) and the generated cases
# fire ZERO — the named tests are the only thing that catches it.

SHAPES = list(itertools.product([False, True], repeat=8))
VICTIMS = ("alpha", "beta", "gamma")

# A note on a host's BODY, below its port line, rather than above its header.
# The `own_comment` axis only ever puts comments directly above a header, and a
# trailing note is a different shape for the comment walk to get wrong.
TRAILING = "# {name} is the PM-630 box. Linear links to this port."


def _lived_in(preamble, own_comment, blank_between, example, crlf,
              example_in_gap, trailing_note, indented=False):
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
        lines += [f"{pad}[hosts.{name}]", f'{pad}kind = "local"',
                  f"{pad}port = {8100 + index}"]
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


# The gap-example assertion is its own test because at HEAD it FAILS, and only
# for the host the example sits under. `without` runs the removal to the next
# `[`, and the walk back from there stops at the blank line below the example —
# so a commented-out block in the gap goes with the host above it. That is D93,
# it is open, and the fix is a change to which side of a gap a comment run
# belongs to, not a line. Recorded here as a strict xfail so it is not lost and
# so closing D93 turns this red rather than silently green.

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
    assert f"{pad}[hosts.{retired}]{ending}" in out, why
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


GAP_SHAPES = [shape for shape in SHAPES if shape[5] and shape[2]]


@pytest.mark.parametrize("shape", GAP_SHAPES)
@pytest.mark.parametrize("victim", VICTIMS)
def test_a_worked_example_in_the_gap_survives_a_removal(shape, victim, request):
    if victim == VICTIMS[0]:
        request.node.add_marker(pytest.mark.xfail(
            strict=True,
            reason="D93: a commented-out block in the gap goes with the host above it",
        ))
    out = without(_lived_in(*shape), victim)
    assert "# [hosts.spare]" in out, (
        f"the worked example in the gap was destroyed\n"
        f"shape={shape} victim={victim}\n{out}"
    )
