"""Run the real toolchain against generated code and report what happened.

THE RULE THIS EXISTS TO ENFORCE
UNKNOWN IS NOT VALID. Three states, and the third is not a polite failure:

    VALID               the toolchain ran and accepted the code
    INVALID             the toolchain ran and rejected it, with the errors
    UNABLE_TO_VALIDATE  we could not run the toolchain -- no backend, install
                        failed, timed out, no tsconfig. This is NOT a pass.

app/validated_fix.py (deleted alongside this module's arrival) got this wrong in the
most expensive way available: it ended `else: return True, ''`, and its TypeScript
check was brace-matching, so it returned VALID for `phoneNumber: int32` AND for
`!!! not rust`. A validator that cannot fail is worse than no validator, because it
converts "unproven" into "proven".

WHY A CONTAINER, AND WHAT THE FALLBACK ACTUALLY COSTS
Validating a customer's repository means installing that repository's dependencies.
`npm` runs `postinstall` scripts by definition, which is arbitrary code execution
from an untrusted source. Two mitigations, in order of strength:

    backend "docker"  install in a container with --ignore-scripts, then typecheck
                      in a SECOND container with --network none, source mounted
                      read-only, memory and cpu capped, no new privileges.
    backend "host"    subprocess with --ignore-scripts, rlimits and a timeout.
                      Network is NOT isolated. This is weaker and is labelled
                      "degraded" in the evidence rather than quietly equivalent.

Every Verdict carries which backend ran, so a reader can tell how much isolation
actually applied. Reporting "VALID" without saying how it was obtained would repeat
the mistake the capability registry was built to stop.

WHAT THIS DELIBERATELY DOES NOT DO
It does not run the consumer's test suite. `tsc --noEmit` is the floor: it catches
the failure mode that shipped, where a removal fix leaves a reference to a field
that no longer exists on the type. Tests come after a compile gate exists, because a
fix that does not compile cannot pass tests either, and the cheaper check should
come first.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from app.capability_claims import ValidationState

#: Pinned. A floating tag would make a verdict unreproducible, and "it validated
#: last week" is not evidence about today.
DOCKER_IMAGE = "node:16-alpine"

INSTALL_TIMEOUT = 300
TYPECHECK_TIMEOUT = 180
MEMORY_LIMIT = "1g"
CPU_LIMIT = "2"

#: tsc diagnostics look like  src/a.ts(19,17): error TS1003: Identifier expected
_TSC_ERROR = re.compile(r"^(?P<file>[^(]+)\((?P<line>\d+),(?P<col>\d+)\):\s+"
                        r"error\s+(?P<code>TS\d+):\s+(?P<message>.*)$")


@dataclass
class Verdict:
    state: ValidationState
    reason: str
    errors: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """Only ever True for a real, successful toolchain run."""
        return self.state is ValidationState.VALID

    def as_detail(self) -> dict:
        return {
            "validation": self.state.value,
            "reason": self.reason,
            "error_count": len(self.errors),
            "errors": self.errors[:5],
            **{f"evidence_{k}": v for k, v in self.evidence.items()},
        }


# --------------------------------------------------------------------------
# backend selection
# --------------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=25).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _host_node() -> str:
    """A node binary that can actually EXECUTE, or "".

    Presence on PATH is not enough: on a glibc 2.26 host the system node exits with
    `GLIBC_2.27 not found`. Checking --version rather than existence is the
    difference between a working fallback and an UNABLE_TO_VALIDATE nobody expected.
    """
    candidates = [shutil.which("node") or ""]
    nvm = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm):
        candidates += [os.path.join(nvm, v, "bin", "node")
                       for v in sorted(os.listdir(nvm), reverse=True)]
    for c in candidates:
        if not c or not os.path.isfile(c):
            continue
        try:
            if subprocess.run([c, "--version"], capture_output=True,
                              timeout=15).returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


#: The host backend is DEGRADED: no network isolation, no cgroup memory/cpu caps, no
#: no-new-privileges. It is a real fallback for a developer machine and a deliberate
#: risk in production, so it is never selected AUTOMATICALLY without this being set.
#:
#: Why a gate rather than just picking it: the production image now ships node, which
#: means choose_backend() would silently start returning "host" for every customer
#: repository -- running `npm install` against dependency trees the customer controls
#: on an unisolated network. `--ignore-scripts` removes the lifecycle-script execution
#: vector and `tsc` does not execute the code it checks, so the residual risk is
#: narrower than "arbitrary code execution" -- but it is not zero, and the difference
#: between a toolchain being AVAILABLE and its weakest mode being ACCEPTED is a
#: decision an operator should make, not an import side effect.
#:
#: An EXPLICIT backend="host" (as tools/verify_validation.py --backend host passes)
#: is unaffected: this gates the automatic fallback only.
DEGRADED_OPT_IN = "RIPPLE_ALLOW_DEGRADED_VALIDATION"


def choose_backend() -> tuple:
    """(backend, note). Preference order is strongest isolation first."""
    if _docker_available():
        return "docker", f"container, {DOCKER_IMAGE}, --network none for typecheck"
    node = _host_node()
    if node:
        if os.environ.get(DEGRADED_OPT_IN, "").strip() not in ("1", "true", "yes"):
            return "", (f"a node toolchain exists ({node}) but the host backend is "
                        f"DEGRADED (no network isolation, no cgroup caps) and "
                        f"{DEGRADED_OPT_IN} is not set. Refusing to select weaker "
                        f"isolation than the operator has accepted")
        return "host", (f"DEGRADED, explicitly accepted via {DEGRADED_OPT_IN}: "
                        f"subprocess with --ignore-scripts, rlimits (2GiB address "
                        f"space, {HOST_CPU_SECONDS}s cpu) and a timeout; network is "
                        f"NOT isolated ({node})")
    return "", "no usable node and no docker"


#: Memoised because `_docker_available()` shells out with a 25s timeout, which has
#: no business running on a health endpoint. Backend availability is a property of
#: the container image and cannot change during a process's lifetime, so caching it
#: is not a staleness risk -- unlike caching a network result, which is the mistake
#: this codebase has made four times.
_BACKEND_DESCRIPTION = None


def describe_backend() -> dict:
    """What THIS host can validate with, as JSON.

    Exists so the DEPLOYED service can state its own capability instead of it being
    inferred from the repository. The repository can claim a cell is AUTO while the
    running image has no TypeScript toolchain at all -- in which case `validate()`
    correctly returns UNABLE_TO_VALIDATE and AUTO can never fire in production. That
    divergence was invisible from either side until this was reported.
    """
    global _BACKEND_DESCRIPTION
    if _BACKEND_DESCRIPTION is None:
        backend, note = choose_backend()
        _BACKEND_DESCRIPTION = {
            "backend": backend or None,
            "isolation": note,
            # The NECESSARY condition for AUTO, not the sufficient one. Whether a
            # given cell reaches AUTO is the registry's decision; this only says
            # whether the toolchain that decision depends on exists here.
            "can_validate": bool(backend),
        }
    return dict(_BACKEND_DESCRIPTION)


# --------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------

#: Address space and CPU ceilings for the HOST backend, mirroring what docker gets
#: from --memory and --cpus. Docker enforces those via cgroups; a bare subprocess
#: gets nothing unless it is asked for, and for three stages this module's docstring
#: and choose_backend() both advertised "rlimits" that did not exist. A claimed
#: protection that is absent is worse than an admitted absence, because it stops
#: anyone looking.
HOST_ADDRESS_SPACE = 2 * 1024 * 1024 * 1024      # 2 GiB; tsc on a large repo is hungry
HOST_CPU_SECONDS = 240
HOST_MAX_FILE_BYTES = 512 * 1024 * 1024          # no filling the disk with one file


def _host_limits():
    """A preexec_fn applying rlimits, or None where unsupported (Windows).

    Deliberately NOT limiting the process count: npm legitimately fans out, and an
    RLIMIT_NPROC low enough to matter breaks the install rather than containing it.
    """
    try:
        import resource
    except ImportError:
        return None

    def _apply():
        for what, limit in ((resource.RLIMIT_AS, HOST_ADDRESS_SPACE),
                            (resource.RLIMIT_CPU, HOST_CPU_SECONDS),
                            (resource.RLIMIT_FSIZE, HOST_MAX_FILE_BYTES)):
            try:
                soft, hard = resource.getrlimit(what)
                ceiling = limit if hard in (resource.RLIM_INFINITY,) else min(limit, hard)
                resource.setrlimit(what, (ceiling, hard))
            except (ValueError, OSError):
                # A limit we cannot set is not a reason to abandon the others.
                pass

    return _apply


def _run(cmd: list, cwd: str, timeout: int, limits=None) -> tuple:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, preexec_fn=limits)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), ""
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s"
    except OSError as exc:
        return None, "", f"could not execute: {exc}"


def _parse_tsc(output: str) -> list:
    out = []
    for line in output.splitlines():
        m = _TSC_ERROR.match(line.strip())
        if m:
            out.append({"file": m.group("file"), "line": int(m.group("line")),
                        "code": m.group("code"), "message": m.group("message")})
    return out


def validate_typescript(workspace: str, backend: str = "",
                        project_subdir: str = "") -> Verdict:
    """Typecheck a TypeScript workspace. The workspace is COPIED, never mutated.

    `workspace` must contain package.json and tsconfig.json -- i.e. a real project,
    because `tsc` cannot resolve imports without the consumer's dependencies and
    compiler options. A single file in isolation typechecks nothing useful.
    """
    backend, note = (backend, "explicit") if backend else choose_backend()
    ev = {"backend": backend or "none", "backend_note": note,
          "image": DOCKER_IMAGE if backend == "docker" else None}

    if not backend:
        return Verdict(ValidationState.UNABLE_TO_VALIDATE,
                       "no validation backend: docker is unreachable and no node "
                       "binary on this host can execute", evidence=ev)

    # An unrecognised backend must NOT fall through to the host path. The first
    # version branched `if backend == "docker": ... else: <host>`, so any unknown
    # string silently ran with the weakest isolation -- the same shape as
    # canonical_op() returning "" for an input it did not recognise, and as a
    # validator that treats "I could not check" as "it is fine".
    if backend not in ("docker", "host"):
        return Verdict(ValidationState.UNABLE_TO_VALIDATE,
                       f"unknown validation backend {backend!r}; expected 'docker' "
                       f"or 'host'. Refusing rather than guessing which isolation "
                       f"level was intended", evidence=ev)

    # WHERE THE MANIFESTS HAVE TO BE, AND WHY THEY ARE NOT ALWAYS TOGETHER.
    #
    # This required package.json AND tsconfig.json in ONE directory. pnpm and yarn
    # workspaces routinely violate that: the package holds tsconfig.json while
    # node_modules and the lockfile sit at the workspace root. Stage 2 measured the
    # consequence -- resolution correctly returned packages/api, and this function
    # then said "package.json is missing", so the most common real monorepo layout
    # was UNABLE_TO_VALIDATE.
    #
    # `workspace` is now where dependencies install (the workspace root) and
    # `project_subdir` is the relative path to the compiler config. Verified in
    # node:16-alpine that this is all it takes, because node's own resolution walks
    # UP from a file looking for node_modules:
    #
    #   npm install                       at the workspace root
    #   ./node_modules/.bin/tsc -p packages/api --noEmit
    #     -> packages/api/src/user.ts(1,14): error TS2322 ...
    #
    # Error paths come back relative to the workspace root, which is what a PR body
    # wants anyway.
    project_dir = os.path.join(workspace, project_subdir) if project_subdir \
        else workspace
    ev["project_subdir"] = project_subdir or "."
    for required, where in (("package.json", workspace),
                            ("tsconfig.json", project_dir)):
        if not os.path.exists(os.path.join(where, required)):
            rel = os.path.relpath(where, workspace)
            return Verdict(ValidationState.UNABLE_TO_VALIDATE,
                           f"{required} is missing from "
                           f"{'the workspace root' if rel == '.' else rel}, so tsc "
                           f"cannot resolve imports or compiler options -- "
                           f"typechecking a file without its project proves nothing",
                           evidence=ev)

    tmp = tempfile.mkdtemp(prefix="ripple-validate-")
    work = os.path.join(tmp, "w")
    try:
        shutil.copytree(workspace, work,
                        ignore=shutil.ignore_patterns("node_modules", ".git"))

        has_lock = os.path.exists(os.path.join(work, "package-lock.json"))
        npm_cmd = ["npm", "ci"] if has_lock else ["npm", "install"]
        npm_cmd += ["--ignore-scripts", "--no-audit", "--no-fund"]
        ev["install"] = " ".join(npm_cmd)

        if backend == "docker":
            install = ["docker", "run", "--rm",
                       "-v", f"{work}:/w", "-w", "/w",
                       "--memory", MEMORY_LIMIT, "--cpus", CPU_LIMIT,
                       "--security-opt", "no-new-privileges",
                       DOCKER_IMAGE] + npm_cmd
        else:
            node_dir = os.path.dirname(_host_node())
            install = ["env", f"PATH={node_dir}:{os.environ.get('PATH','')}"] + npm_cmd

        # rlimits apply ONLY to the host path; docker enforces the equivalent via
        # cgroups and adding a preexec_fn there would limit the `docker` client.
        host_limits = _host_limits() if backend == "host" else None
        ev["host_rlimits"] = bool(host_limits)

        code, out, err = _run(install, work, INSTALL_TIMEOUT, host_limits)
        ev["install_exit"] = code
        if code != 0:
            # NOT invalid. We never found out whether the code is correct.
            return Verdict(ValidationState.UNABLE_TO_VALIDATE,
                           f"dependency install failed ({err or 'exit ' + str(code)}), "
                           f"so the toolchain never ran: {out.strip()[-300:]}",
                           evidence=ev)

        # -p targets the project rather than the cwd. Without it tsc reads the
        # cwd's tsconfig, which in a hoisted workspace is the ROOT one -- and
        # that either excludes the changed package or drags in every package.
        _project_arg = ["-p", project_subdir] if project_subdir else []

        if backend == "docker":
            # Second container: no network at all, source read-only.
            check = ["docker", "run", "--rm", "--network", "none",
                     "-v", f"{work}:/w:ro", "-w", "/w",
                     "--memory", MEMORY_LIMIT, "--cpus", CPU_LIMIT,
                     "--security-opt", "no-new-privileges",
                     DOCKER_IMAGE,
                     "./node_modules/.bin/tsc", "--noEmit", "--skipLibCheck"] + _project_arg
        else:
            node_dir = os.path.dirname(_host_node())
            check = ["env", f"PATH={node_dir}:{os.environ.get('PATH','')}",
                     "./node_modules/.bin/tsc", "--noEmit", "--skipLibCheck"] + _project_arg
        ev["typecheck"] = "tsc --noEmit --skipLibCheck " + " ".join(_project_arg)

        code, out, err = _run(check, work, TYPECHECK_TIMEOUT, host_limits)
        ev["typecheck_exit"] = code
        if code is None:
            return Verdict(ValidationState.UNABLE_TO_VALIDATE,
                           f"typecheck could not run: {err}", evidence=ev)

        errors = _parse_tsc(out)
        if code == 0:
            return Verdict(ValidationState.VALID,
                           "tsc --noEmit accepted the generated code", evidence=ev)
        return Verdict(ValidationState.INVALID,
                       f"tsc rejected the generated code with {len(errors) or '?'} "
                       f"error(s)",
                       errors=[f"{e['file']}({e['line']}): {e['code']} {e['message']}"
                               for e in errors] or [out.strip()[-300:]],
                       evidence=ev)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: language -> runner. A language absent here is UNABLE_TO_VALIDATE, which is the
#: honest state and makes no claim -- the same reason capability_claims lists a
#: validator for only three languages instead of inventing eleven more.
RUNNERS = {
    "typescript": validate_typescript,
}


def validate(language: str, workspace: str, backend: str = "",
             project_subdir: str = "") -> Verdict:
    runner = RUNNERS.get(language)
    if runner is None:
        return Verdict(
            ValidationState.UNABLE_TO_VALIDATE,
            f"no validation runner for {language} -- see app/validation.py RUNNERS",
            evidence={"backend": "none", "language": language})
    return runner(workspace, backend=backend,
                  project_subdir=project_subdir)
