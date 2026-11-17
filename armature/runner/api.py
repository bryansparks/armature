"""What a remote runner provider builds on, in one place. A provider imports only from here.

A provider runs each claimed job somewhere other than this host, such as a container service. It subclasses
``Runner`` and overrides its public methods:

``platform()``
    Where its jobs run. Return ``location.make(REMOTE, "<label>", same_user=True)``, so runs read as the
    provider's own label. Inside each run, set ``LOCATION_KIND_ENV`` to ``REMOTE`` and ``LOCATION_LABEL_ENV`` to the
    label, so the run says the same.
``hello()``, ``beat(job_id)``, ``claim(answer_headers)``
    Read what the server answers. Each hello and heartbeat answer has ``max_slots`` when the instance sets
    ``runner.max_slots``, and so does each claim answer's ``MAX_SLOTS_HEADER``. A host runner ignores it.
``fill()``
    Claim jobs. Override it to hold back claims while the provider can't take them.
``clear_leftovers()``
    Clear what a runner that stopped mid-job left in ``home``, such as work still running on the provider.
``run_job(bundle)``
    Run one claimed job, heartbeat it with ``beat``, relay its events with ``Client.events``, and report how it
    ended with ``finish(job_id, status, error, paused)``. ``paused(job_id)`` says whether the server heard the run
    stop at a gate, ``clock()`` is the runner's monotonic clock, and ``stopping`` is set once the runner stops.

The runner offers a ``remote:<name>`` capability. A runner offering one takes only jobs whose ``requires`` names
it, and the server starts no AWS environment for such a job. Set ``GIT_VIA_PROXY_ENV`` to ``"1"`` inside a run whose
git reaches its hosts through a proxy that adds the credentials, and ``TOOLS_VIA_PROXY_ENV`` to ``"1"`` inside a run
that reaches the server only through its inherited proxy. A provider that can't pause a run at a gate passes
``pauses=False`` to ``Runner``, which says so at hello, and the server then gives it no job that may pause or that
resumes from a gate. Only such a provider sets ``GATE_AUTO_APPROVE_ENV`` to ``"1"`` inside its runs. Every name for
which ``host_only`` holds, the git config and proxy names among them, is dropped from a job's settings and secrets,
so set these after that.

``serve(make, server=...)`` starts a runner from the provider's own command, as ``armature runner`` does.
"""
from __future__ import annotations

from armature.events import location
from armature.events.location import LOCATION_KIND_ENV, LOCATION_LABEL_ENV, REMOTE
from armature.events.redact import REDACT_ENV, env_values, redact_text
from armature.nodes.gate import (
    CHECKPOINT_FILE,
    GATE_AUTO_APPROVE_ENV,
    GATE_DIR_ENV,
    GATE_FILE,
    read_gate,
)
from armature.nodes.harness import (
    GIT_VIA_PROXY_ENV,
    HOST_ONLY_ENV,
    MODELS_ENV,
    ROLES_ENV,
    SAME_USER_ENV,
    SESSIONS_ENV,
    SKILLS_ENV,
    TOOLS_VIA_PROXY_ENV,
    host_only,
)
from armature.packs import BUILTIN_PACKS, PACKS_PATH_ENV
from armature.packs.tool import URL_ENV as TOOLS_URL_ENV
from armature.runner.client import (
    END_GRACE_S,
    END_POLL_S,
    HELD,
    JOB_PREFIX,
    MAX_SLOTS_HEADER,
    OPEN,
    Client,
    Runner,
    RunnerLocked,
    ServerError,
)
from armature.runner.process import (
    BUNDLE_VERSION,
    LOG_FILE,
    ROLES_DIR,
    SHARED_PREFIX,
    SKILLS_DIR,
    armature_binary,
    check_bundle_version,
    job_path,
    popen,
    tail,
    write_bundle,
)
from armature.runner.serving import (
    DEFAULT_KEY_ENV,
    DEFAULT_KEY_FILE,
    check_name,
    check_server,
    fail,
    parse_labels,
    serve,
)
from armature.spec.models import CAPABILITY, REMOTE_PREFIX, remote_capabilities
from armature.state.work import write_atomic

# Bumped when a change here would break a provider built against an earlier one.
PROVIDER_API = 1

__all__ = [
    "BUILTIN_PACKS", "BUNDLE_VERSION", "CAPABILITY", "CHECKPOINT_FILE", "DEFAULT_KEY_ENV", "DEFAULT_KEY_FILE",
    "END_GRACE_S", "END_POLL_S", "GATE_AUTO_APPROVE_ENV", "GATE_DIR_ENV", "GATE_FILE", "GIT_VIA_PROXY_ENV", "HELD",
    "HOST_ONLY_ENV", "JOB_PREFIX", "LOCATION_KIND_ENV", "LOCATION_LABEL_ENV", "LOG_FILE", "MAX_SLOTS_HEADER",
    "MODELS_ENV", "OPEN", "PACKS_PATH_ENV", "PROVIDER_API", "REDACT_ENV", "REMOTE", "REMOTE_PREFIX", "ROLES_DIR",
    "ROLES_ENV", "SAME_USER_ENV", "SESSIONS_ENV", "SHARED_PREFIX", "SKILLS_DIR", "SKILLS_ENV", "TOOLS_URL_ENV",
    "TOOLS_VIA_PROXY_ENV",
    "Client", "Runner", "RunnerLocked", "ServerError", "armature_binary", "check_bundle_version", "check_name",
    "check_server", "env_values", "fail", "job_path", "location", "parse_labels", "popen", "read_gate",
    "host_only", "redact_text", "remote_capabilities", "serve", "tail", "write_atomic", "write_bundle",
]
