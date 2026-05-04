# Shadow-mode runner for plan-review.
#
# Spawned detached by chain._dispatch_shadow_runs after the primary chain
# returns. Reads a JSON job descriptor (prompt + metadata + provider list +
# primary_provider name) from a temp file, runs each shadow provider via
# chain._try_provider, and lets _try_provider write the cycle log with
# shadow=true + primary_provider set.
#
# Best-effort by design: any error here is silent (stdout/stderr go to
# DEVNULL in the parent). The point is to collect comparison data, not to
# affect the user's plan-review loop.
#
# Lifecycle: parent hook exits as soon as it writes its decision JSON; this
# process keeps running thanks to start_new_session=True. Job file is
# deleted after all providers complete.

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _lib import chain  # noqa: E402
from _lib.chain import (  # noqa: E402
    DEFAULT_PROVIDER_TIMEOUT,
    PROVIDER_TIMEOUTS,
    _file_log,
    _try_provider,
)


def _run_job(job_path: Path) -> int:
    try:
        job = json.loads(job_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        # Can't even read the job — nowhere to log to about a specific plan.
        # _file_log suppresses its own OSErrors.
        _file_log(f"shadow runner: failed to read {job_path}: {exc}")
        return 1

    prompt = job.get("prompt", "")
    metadata = job.get("metadata") or {}
    primary_provider = job.get("primary_provider") or None
    providers = job.get("providers") or []

    if not prompt or not providers:
        _file_log(f"shadow runner: empty job ({job_path})")
        return 0

    _file_log(
        f"shadow runner start: providers={providers} primary={primary_provider} "
        f"prompt_size={len(prompt)} job={job_path}",
    )

    for provider in providers:
        if provider not in chain.PROVIDER_CMDS:
            _file_log(f"shadow runner: skipping unknown provider {provider!r}")
            continue
        timeout = PROVIDER_TIMEOUTS.get(provider, DEFAULT_PROVIDER_TIMEOUT)
        try:
            _try_provider(
                provider,
                prompt,
                timeout=timeout,
                metadata=metadata,
                shadow=True,
                primary_provider=primary_provider,
            )
        except Exception as exc:
            _file_log(f"shadow runner: {provider} crashed: {exc}")

    _file_log(f"shadow runner done: job={job_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return 2

    job_path = Path(argv[0])
    rc = _run_job(job_path)

    # Tidy up the job file regardless of outcome — shadow logs are the
    # durable artifact, the job descriptor is ephemeral.
    with contextlib.suppress(OSError):
        job_path.unlink()
    return rc


if __name__ == "__main__":
    sys.exit(main())
