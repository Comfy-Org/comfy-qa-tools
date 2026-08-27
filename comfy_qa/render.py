"""Output shapes. The QA playbooks disagree on the evidence format, so emit all
three rather than picking a house style and making testers retype."""

from __future__ import annotations

from .env import EnvReport, flag_diff, release_line


def _flags_on(r: EnvReport) -> list[str]:
    return sorted(k for k, v in r.flags.items() if v)


def _known(value: str | None) -> str:
    """What to print for a field the environment did not report.

    A build older than `comfyui_version` leaves that field empty, and an
    evidence block reading "ComfyUI None" has been pasted into a report as if
    it meant something. Say unknown, and say it the same way everywhere.
    """
    return value if value else "unknown"


def table(reports: list[EnvReport]) -> str:
    lines = []
    for r in reports:
        if r.error:
            lines.append(f"{r.name:<15} !! {r.error}")
        elif r.kind == "local":
            fe = r.frontend_installed or "?"
            warn = f"  << MISMATCH, core wants {r.frontend_required}" if r.frontend_mismatch else ""
            lines.append(
                f"{r.name:<15} ComfyUI {_known(r.comfyui_version)}  frontend {fe}{warn}")
        else:
            when = r.commit_date or ""
            what = r.commit_subject or ""
            if len(what) > 60:
                what = what[:57] + "..."
            lines.append(f"{r.name:<15} {r.short_sha}  {when}  {what}")
    out = ["", *lines, ""]

    diff = flag_diff(reports)
    cloud_with_flags = [r for r in reports if r.kind == "cloud" and r.flags]
    if cloud_with_flags:
        # The union, not the first environment's count: when two environments
        # disagree about which flags *exist*, quoting one of their totals
        # understates what was actually compared.
        n = len({k for r in cloud_with_flags for k in r.flags})
        if diff:
            out.append(f"{n} flags checked, {len(diff)} differ across environments:")
            for k, per in diff.items():
                out.append("  " + f"{k:<38}" + "  ".join(
                    f"{env}={'ON' if val else 'OFF'}" for env, val in per.items()))
        elif len(cloud_with_flags) < 2:
            # Nothing was compared. "all identical across environments" off a
            # single probe is a claim this run did not earn.
            out.append(f"{n} flags checked, nothing to compare against")
        else:
            out.append(f"{n} flags checked, all identical across environments")
        out.append("")

    line = _local_vs_cloud(reports)
    if line:
        out += [line, ""]
    return "\n".join(out)


def _local_vs_cloud(reports: list[EnvReport]) -> str | None:
    """The trap the guide warns about: testing the local half on the wrong frontend."""
    local = next((r for r in reports if r.kind == "local" and not r.error), None)
    if not local or not local.frontend_installed:
        return None
    cloud_line = release_line(reports)
    if not cloud_line:
        return None
    local_line = ".".join(local.frontend_installed.split(".")[:2])
    if local_line != cloud_line:
        return (f"WARNING  local frontend is {local.frontend_installed} but cloud is on the "
                f"{cloud_line} line — the local half of a {cloud_line} plan would test the wrong build")
    return None


def _flag_summary(r: EnvReport, baseline: EnvReport | None, only: list[str] | None) -> str:
    """Only the flags worth pasting.

    Listing all 32 is unusable in a Slack line, and the guide's own example lists
    two. Default to what makes this environment *unusual* — the flags that differ
    from production — and give a count for the rest.
    """
    if not r.flags:
        return "unknown"
    if only:
        picked = [(k, r.flags.get(k)) for k in only if k in r.flags]
        return " + ".join(f"{k} {'ON' if v else 'OFF'}" for k, v in picked) or "none matched"
    if baseline and baseline.flags and baseline is not r:
        differing = [k for k in sorted(r.flags) if r.flags[k] != baseline.flags.get(k)]
        if differing:
            shown = " + ".join(f"{k} {'ON' if r.flags[k] else 'OFF'}" for k in differing)
            return f"{shown} (differs from {baseline.name}; {len(r.flags)} flags checked)"
        return f"same as {baseline.name} ({len(r.flags)} flags checked)"
    on = sum(1 for v in r.flags.values() if v)
    return f"{on}/{len(r.flags)} flags on"


def evidence(r: EnvReport, *, platform: str | None = None,
             baseline: EnvReport | None = None, only: list[str] | None = None) -> dict[str, str]:
    """The three shapes the playbooks use. Same capture, three formats.

    An environment that failed to answer gets an evidence block that says so.
    The alternative — filling the build in as "None" and the flags as
    "unknown" — produces something that looks exactly like a real capture and
    gets pasted into a report as one.
    """
    if r.error:
        build = version = f"NOT PROBED — {r.error}"
        flags = "not probed"
    elif r.kind == "local":
        version = _known(r.comfyui_version)
        build = f"ComfyUI {version} · frontend {_known(r.frontend_installed)}"
        flags = "n/a (local)"
    else:
        version = _known(r.short_sha)
        build = f"build {version}"
        flags = _flag_summary(r, baseline, only)

    plat = f" · {platform}" if platform else ""
    return {
        # 1. onboarding guide one-liner, for pasting under a Slack finding
        "oneline": f"{r.name} · {build} · {flags}{plat}",
        # 2. bug-tracker row fields
        "tracker": "\n".join([
            f"Environment: {r.name}",
            f"Build: {version}",
            f"Flags: {flags}",
            f"Platform: {platform or '(fill in)'}",
        ]),
        # 3. Custom Node Playbook template header
        "playbook": "\n".join([
            f"Environment: {r.name} ({r.url})",
            f"Build: {build}",
            f"Flags: {flags}",
            f"Platform: {platform or '(fill in)'}",
            "Works locally: (yes / no / not checked)",
            "Regression: (yes / no / unknown)",
        ]),
    }


def as_dict(reports: list[EnvReport]) -> dict:
    return {
        "environments": [
            {k: v for k, v in {
                "name": r.name, "kind": r.kind, "url": r.url,
                "sha": r.sha, "short_sha": r.short_sha,
                "commit_date": r.commit_date, "commit_subject": r.commit_subject,
                "comfyui_version": r.comfyui_version,
                "frontend_installed": r.frontend_installed,
                "frontend_required": r.frontend_required,
                "frontend_mismatch": r.frontend_mismatch if r.kind == "local" else None,
                "flags_on": _flags_on(r) if r.flags else None,
                "flag_count": len(r.flags) or None,
                "error": r.error,
            }.items() if v is not None}
            for r in reports
        ],
        "flag_diff": flag_diff(reports),
        "warning": _local_vs_cloud(reports),
    }
