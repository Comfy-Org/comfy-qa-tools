"""v0 — report which build every environment is serving, and its flag state.

The one check the QA onboarding guide calls out as catching "more wasted days
than anything else": a failed deploy leaves the old version running and looks
completely normal. Cloud environments expose the build as an ``x-frontend-version``
response header; a local ComfyUI does not, but reports more via ``/system_stats``.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

CLOUD_ENVS = {
    "testcloud": "https://testcloud.comfy.org",
    "stagingcloud": "https://stagingcloud.comfy.org",
    "cloud": "https://cloud.comfy.org",
}
LOCAL_DEFAULT = "http://127.0.0.1:8188"
FRONTEND_REPO = "Comfy-Org/ComfyUI_frontend"
TIMEOUT = 10

# /api/features also carries Firebase / PostHog / Mixpanel / Sentry / Churnkey
# config. Only booleans are ever printed — a whitelist by TYPE, so a new secret
# added upstream can never leak into a pasted evidence block.
def _boolean_flags(payload: dict) -> dict[str, bool]:
    return {k: v for k, v in payload.items() if isinstance(v, bool)}


@dataclass
class EnvReport:
    name: str
    url: str
    kind: str  # "cloud" | "local"
    sha: str | None = None
    commit_date: str | None = None
    commit_subject: str | None = None
    flags: dict[str, bool] = field(default_factory=dict)
    comfyui_version: str | None = None
    frontend_installed: str | None = None
    frontend_required: str | None = None
    error: str | None = None

    @property
    def short_sha(self) -> str | None:
        return self.sha[:8] if self.sha else None

    @property
    def frontend_mismatch(self) -> bool:
        """Local only: the installed frontend is not the one this core wants."""
        return bool(
            self.frontend_installed
            and self.frontend_required
            and self.frontend_installed != self.frontend_required
        )


# The CDN in front of the cloud environments rejects urllib's default
# ``Python-urllib/3.x`` agent with a 403, so send a real one.
USER_AGENT = "comfy-qa-tools/0.1 (+https://github.com/Comfy-Org/comfy-qa-tools)"


def _get(url: str, *, head: bool = False):
    req = urllib.request.Request(
        url, method="HEAD" if head else "GET", headers={"User-Agent": USER_AGENT}
    )
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _resolve_sha(sha: str) -> tuple[str | None, str | None]:
    """Turn a bare SHA into (date, subject) via gh. Best-effort — never fatal."""
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{FRONTEND_REPO}/commits/{sha}",
             "--jq", '.commit.author.date[0:10] + "\\t" + (.commit.message | split("\\n")[0])'],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return None, None
        date, _, subject = out.stdout.strip().partition("\t")
        return date or None, subject or None
    except (OSError, subprocess.SubprocessError):
        return None, None


def probe_cloud(name: str, url: str, *, resolve: bool = True) -> EnvReport:
    r = EnvReport(name=name, url=url, kind="cloud")
    try:
        with _get(url + "/", head=True) as resp:
            r.sha = resp.headers.get("x-frontend-version")
    except (urllib.error.URLError, OSError) as e:
        r.error = f"unreachable: {e}"
        return r
    if not r.sha:
        r.error = "no x-frontend-version header"
    elif resolve:
        r.commit_date, r.commit_subject = _resolve_sha(r.sha)
    try:
        with _get(url + "/api/features") as resp:
            r.flags = _boolean_flags(json.load(resp))
    except (urllib.error.URLError, OSError, ValueError):
        pass  # flags are a bonus; a missing endpoint is not a failed probe
    return r


def probe_local(url: str = LOCAL_DEFAULT) -> EnvReport:
    r = EnvReport(name="local", url=url, kind="local")
    try:
        with _get(url + "/system_stats") as resp:
            stats = json.load(resp).get("system", {})
    except (urllib.error.URLError, OSError, ValueError) as e:
        r.error = f"not running ({e})"
        return r
    r.comfyui_version = stats.get("comfyui_version")
    r.frontend_required = stats.get("required_frontend_version")
    for pkg in stats.get("comfy_package_versions") or []:
        if pkg.get("name") == "comfyui-frontend-package":
            r.frontend_installed = pkg.get("installed")
    return r


def collect(targets: list[str] | None = None, *, local: bool = True,
            local_url: str = LOCAL_DEFAULT, resolve: bool = True) -> list[EnvReport]:
    names = targets or list(CLOUD_ENVS)
    reports = [probe_cloud(n, CLOUD_ENVS[n], resolve=resolve) for n in names if n in CLOUD_ENVS]
    if local and (not targets or "local" in targets):
        reports.append(probe_local(local_url))
    return reports


def flag_diff(reports: list[EnvReport]) -> dict[str, dict[str, bool]]:
    """Flags whose value is not identical across every cloud environment probed."""
    cloud = [r for r in reports if r.kind == "cloud" and r.flags]
    if len(cloud) < 2:
        return {}
    keys = {k for r in cloud for k in r.flags}
    return {
        k: {r.name: r.flags.get(k) for r in cloud}
        for k in sorted(keys)
        if len({r.flags.get(k) for r in cloud}) > 1
    }


def release_line(reports: list[EnvReport]) -> str | None:
    """The frontend version the cloud side is on, if a commit subject names one."""
    import re
    for r in reports:
        if r.kind == "cloud" and r.commit_subject:
            m = re.search(r"\b(\d+\.\d+)(?:\.\d+)?\b", r.commit_subject)
            if m:
                return m.group(1)
    return None
