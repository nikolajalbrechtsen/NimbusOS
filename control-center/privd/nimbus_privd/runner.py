# =============================================================================
# nimbus_privd/runner.py: how privd runs programs
# =============================================================================
# What: run() and start() take an argument list whose first element is an
#       absolute path; there is no shell anywhere. The environment is a
#       fixed minimal one (PATH, C.UTF-8 locale). Secrets go in on stdin
#       (input=...), never as arguments. CommandFailed carries the exit
#       status and the last lines of output for the error message.
# Why:  docs/PLAN.md §4.3: "calls programs with argument lists (absolute
#       paths, never a shell)". Tests replace run/start on this module.
# =============================================================================

import subprocess

SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


class CommandFailed(Exception):
    def __init__(self, argv, returncode, output):
        self.argv = list(argv)
        self.returncode = returncode
        self.output = output
        tail = " ".join(output.strip().splitlines()[-3:])[:400]
        super().__init__(f"{argv[0].rsplit('/', 1)[-1]} failed (exit status {returncode})"
                         + (f": {tail}" if tail else ""))


def _check_argv(argv):
    if not argv or not isinstance(argv[0], str) or not argv[0].startswith("/"):
        raise ValueError("commands must start with an absolute path")
    if any(not isinstance(a, str) for a in argv):
        raise ValueError("command arguments must be strings")


def run(argv, *, input=None, timeout=60, check=True, env=None):
    """Run ARGV to completion; return the CompletedProcess (text mode)."""
    _check_argv(argv)
    proc = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout,
                          env={**SAFE_ENV, **(env or {})}, shell=False, check=False)
    if check and proc.returncode != 0:
        raise CommandFailed(argv, proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
    return proc


def start(argv, *, env=None):
    """Start ARGV with stdout (and stderr) as a text pipe; caller reads it."""
    _check_argv(argv)
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**SAFE_ENV, **(env or {})}, shell=False, bufsize=1)
