"""Bounded, read-only process inspection used by terminal closing.

The closing path must be able to tell an idle shell from a shell with a
foreground job without guessing from the terminal title or from CPU usage.
This module deliberately keeps that observation small: it asks ``ps`` for an
allowlist of fields, never invokes a shell, and never stores the command line
or environment.  Tests can inject a runner or use the small record objects
directly; the lifecycle code does not need to know how a process was found.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import posixpath
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping


# ``lstart`` is retained as a single opaque value.  It is useful as the
# process-start component of an identity and avoids relying on a PID being
# unique after a process exits.  ``command`` is parsed only to its executable
# basename and is never returned to callers.
PS_FIELDS = ("pid", "ppid", "pgid", "tpgid", "tty", "state", "lstart", "command")


def _positive_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _optional_pid(value: Any) -> int | None:
    if value in (None, "", "-"):
        return None
    return _positive_pid(value)


def _clean_text(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Stable process identity components safe to retain in a receipt.

    ``start_time`` comes from ``ps lstart``.  A PID without a start time is
    intentionally not considered a stable identity by
    :meth:`is_stable`.
    """

    pid: int
    start_time: str | None = None
    tty: str | None = None

    @property
    def start(self) -> str | None:
        """Compatibility alias used by older process adapters."""

        return self.start_time

    @property
    def is_stable(self) -> bool:
        return self.pid > 0 and bool(self.start_time)

    def as_dict(self) -> dict[str, Any]:
        # Keep both spellings while adapters migrate.  They contain the same
        # opaque value and are not a process command or environment dump.
        result: dict[str, Any] = {"pid": self.pid}
        if self.start_time is not None:
            result["start_time"] = self.start_time
            result["start"] = self.start_time
            result["starttime"] = self.start_time
        if self.tty is not None:
            result["tty"] = self.tty
        return result


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    """Allowlisted process facts returned by :class:`MacOSProcessProvider`."""

    pid: int
    ppid: int | None = None
    pgid: int | None = None
    tpgid: int | None = None
    tty: str | None = None
    state: str | None = None
    start_time: str | None = None
    executable: str | None = None

    @property
    def identity(self) -> ProcessIdentity:
        return ProcessIdentity(self.pid, self.start_time, self.tty)

    @property
    def foreground(self) -> bool | None:
        if self.pgid is None or self.tpgid is None or self.pgid <= 0:
            return None
        return self.pgid == self.tpgid

    @property
    def stopped(self) -> bool:
        return bool(self.state) and self.state[0].upper() in {"T", "W"}

    @property
    def exited(self) -> bool:
        return bool(self.state) and self.state[0].upper() in {"Z", "X"}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "identity": self.identity.as_dict(),
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "tpgid": self.tpgid,
            "tty": self.tty,
            "state": self.state,
            "foreground": self.foreground,
        }
        if self.executable is not None:
            result["executable"] = self.executable
        return result


def _executable_name(command: str | None) -> str | None:
    """Return only a basename from ``ps`` output.

    This deliberately does not preserve arguments.  In particular, command
    arguments can contain credentials or terminal content and are not useful
    for lifecycle identity.
    """

    command = _clean_text(command)
    if not command:
        return None
    first = command.split(None, 1)[0]
    first = first.lstrip("-")
    return posixpath.basename(first) or None


def _parse_ps_line(line: str) -> ProcessRecord | None:
    # The first six fields are single-token values.  lstart is five tokens on
    # macOS (weekday, month, day, time, year); the remainder is command text.
    fields = line.strip().split(None, 11)
    if len(fields) < 7:
        return None
    pid = _positive_pid(fields[0])
    if pid is None:
        return None
    ppid = _optional_pid(fields[1]) if len(fields) > 1 else None
    pgid = _optional_pid(fields[2]) if len(fields) > 2 else None
    tpgid = _optional_pid(fields[3]) if len(fields) > 3 else None
    tty = _clean_text(fields[4]) if len(fields) > 4 else None
    state = _clean_text(fields[5]) if len(fields) > 5 else None

    remainder = fields[6:]
    if len(remainder) >= 6:
        start_time = " ".join(remainder[:5])
        command = remainder[5]
    else:
        # Test doubles and older ps implementations may use a one-token
        # start format.  Keep it opaque and still parse the executable.
        start_time = remainder[0] if remainder else None
        command = remainder[1] if len(remainder) > 1 else None
    return ProcessRecord(
        pid=pid,
        ppid=ppid,
        pgid=pgid,
        tpgid=tpgid,
        tty=tty,
        state=state,
        start_time=start_time,
        executable=_executable_name(command),
    )


class MacOSProcessProvider:
    """Inspect process topology with a safe, argv-only ``ps`` invocation.

    The provider is intentionally read-only.  It exposes no generic signal
    method, so a caller must supply an explicit interrupt/recovery adapter
    before any terminal input or process interruption can occur.
    """

    def __init__(
        self,
        *,
        runner: Callable[..., Any] | None = None,
        own_pid: int | None = None,
        timeout: float = 1.0,
        enabled: bool | None = None,
        signaler: Callable[[int, int], Any] | None = None,
        verify_timeout: float = 0.5,
    ) -> None:
        self._runner = runner or subprocess.run
        self.own_pid = _positive_pid(own_pid) or os.getpid()
        self.timeout = timeout
        self.enabled = sys.platform == "darwin" if enabled is None else bool(enabled)
        self._signaler = signaler or os.kill
        self.verify_timeout = max(0.05, float(verify_timeout))

    @staticmethod
    def _argv(pid: int | None = None) -> list[str]:
        spec = ",".join(f"{field}=" for field in PS_FIELDS)
        argv = ["ps", "-ww"]
        if pid is None:
            argv.extend(("-ax", "-o", spec))
        else:
            argv.extend(("-p", str(pid), "-o", spec))
        return argv

    def _run(self, argv: list[str]) -> str:
        if not self.enabled:
            return ""
        try:
            completed = self._runner(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError, TypeError):
            return ""
        if isinstance(completed, str):
            return completed
        return getattr(completed, "stdout", "") or ""

    def list_processes(self) -> list[ProcessRecord]:
        """Return records for the current process table."""

        output = self._run(self._argv())
        return [record for line in output.splitlines() if (record := _parse_ps_line(line))]

    def get(self, pid: int | str | None) -> ProcessRecord | None:
        parsed = _positive_pid(pid)
        if parsed is None:
            return None
        output = self._run(self._argv(parsed))
        for line in output.splitlines():
            record = _parse_ps_line(line)
            if record is not None and record.pid == parsed:
                return record
        return None

    @staticmethod
    def _descendants(pid: int, records: Iterable[ProcessRecord]) -> set[int]:
        children: dict[int, list[int]] = {}
        for record in records:
            if record.ppid is not None:
                children.setdefault(record.ppid, []).append(record.pid)
        result: set[int] = set()
        todo = [pid]
        while todo:
            parent = todo.pop()
            for child in children.get(parent, ()):
                if child not in result:
                    result.add(child)
                    todo.append(child)
        return result

    def ancestor_pids(
        self,
        pid: int | str | None,
        records: Iterable[ProcessRecord] | None = None,
    ) -> frozenset[int]:
        """Return ``pid`` and its observable parent chain."""

        parsed = _positive_pid(pid)
        if parsed is None:
            return frozenset()
        records_list = list(records) if records is not None else self.list_processes()
        by_pid = {record.pid: record for record in records_list}
        result: set[int] = {parsed}
        current = parsed
        seen: set[int] = set()
        while current not in seen:
            seen.add(current)
            record = by_pid.get(current)
            if record is None or record.ppid is None or record.ppid <= 0:
                break
            result.add(record.ppid)
            current = record.ppid
        return frozenset(result)

    def protected_pids(
        self, records: Iterable[ProcessRecord] | None = None
    ) -> frozenset[int]:
        """Return the MCP process and every observed ancestor PID."""

        return self.ancestor_pids(self.own_pid, records)

    def inspect(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        """Describe the target process and any active/stopped descendants.

        The returned mapping is deliberately a small evidence object.  A
        missing process table is represented by ``None`` so callers can fail
        closed rather than treating an empty table as an idle shell.
        """

        if not self.enabled:
            return None
        process_info = row.get("process") if isinstance(row, Mapping) else None
        job_pid: int | None = None
        if isinstance(process_info, Mapping):
            job_pid = _positive_pid(
                process_info.get("pid") or process_info.get("job_pid")
            )
        if job_pid is None and isinstance(row, Mapping):
            job_pid = _positive_pid(
                row.get("job_pid")
                or row.get("jobPid")
                or _nested_value(row.get("context"), "job_pid")
            )
        records = self.list_processes()
        if not records:
            return None
        by_pid = {record.pid: record for record in records}
        target = by_pid.get(job_pid) if job_pid is not None else None
        target_is_shell = bool(
            target is not None
            and target.executable
            in {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "pwsh", "powershell"}
        )
        tty = target.tty if target is not None else _row_tty(row)
        if tty is None:
            # Without a controlling TTY there is no bounded way to separate
            # this session's jobs from unrelated processes.  Preserve a
            # stable identity for diagnostics, but omit job evidence so the
            # closing backend remains ``unknown``.
            return {
                "available": False,
                "identity": target.identity.as_dict() if target is not None else None,
                "pid": target.pid if target is not None else job_pid,
                "tty": None,
            }
        shell_names = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "pwsh", "powershell"}
        if target_is_shell:
            # Some shell integrations report the shell PID while a child job
            # owns the foreground process group.  Bind the operation to that
            # child so force interruption targets its exact foreground group.
            foreground_jobs = [
                record
                for record in records
                if record.tty == target.tty
                and record.executable not in shell_names
                and record.foreground is True
                and not record.exited
            ]
            leaders = [record for record in foreground_jobs if record.pid == record.pgid]
            if len(leaders) == 1:
                target = leaders[0]
            elif len(foreground_jobs) == 1:
                target = foreground_jobs[0]
        candidate_ids = self._descendants(job_pid, records) if job_pid is not None else set()
        shell_pid = job_pid if target_is_shell else None
        if shell_pid is None and target is not None:
            # jobPid can point at the foreground program rather than the
            # shell.  Walk its observed ancestry to identify the shell root.
            by_pid = {record.pid: record for record in records}
            current = target
            seen: set[int] = set()
            while current.pid not in seen:
                seen.add(current.pid)
                if current.executable in shell_names:
                    shell_pid = current.pid
                    break
                if current.ppid is None:
                    break
                current = by_pid.get(current.ppid)
                if current is None:
                    break
        candidates = []
        # When the foreground PID has just exited, ``jobPid`` can briefly
        # retain that old PID while the shell remains on the same TTY.  Find
        # the shell root from the TTY itself so the shell is not misclassified
        # as a new active job.  A root is a shell whose observed ancestry has
        # no other shell on this TTY; if there are multiple roots we leave
        # them in the candidate set and fail closed as busy/unknown.
        shell_pids = {
            record.pid
            for record in records
            if record.tty == tty and record.executable in shell_names
        }
        if shell_pid is None and len(shell_pids) > 0:
            roots: set[int] = set()
            for candidate in shell_pids:
                current = by_pid.get(candidate)
                seen: set[int] = set()
                has_shell_parent = False
                while current is not None and current.pid not in seen:
                    seen.add(current.pid)
                    parent_pid = current.ppid
                    if parent_pid is None:
                        break
                    parent = by_pid.get(parent_pid)
                    if parent is None:
                        break
                    if parent.pid in shell_pids:
                        has_shell_parent = True
                        break
                    current = parent
                if not has_shell_parent:
                    roots.add(candidate)
            if len(roots) == 1:
                shell_pid = next(iter(roots))

        for record in records:
            if record.exited:
                continue
            descendant = record.pid in candidate_ids or record.pid == job_pid
            # A descendant remains owned by this terminal session even after
            # it calls setsid/detaches and loses the shell TTY.  Same-TTY
            # processes outside the descendant set are still treated as
            # session jobs so background siblings cannot slip through.
            if not descendant and tty is not None and record.tty != tty:
                continue
            if shell_pid is not None and record.pid == shell_pid:
                continue
            # Keep descendants and same-TTY jobs.  A same-TTY process outside
            # the foreground group is a background job and still blocks an
            # ordinary close; shell ancestors are the only benign processes
            # excluded here.
            if descendant or record.tty == tty:
                if record.executable in shell_names and shell_pid is not None and record.pid == shell_pid:
                    continue
                candidates.append(record)
        active = [record for record in candidates if not record.stopped]
        stopped = [record for record in candidates if record.stopped]
        protected = self.protected_pids(records)
        protected_groups = {
            record.pgid
            for record in records
            if record.pid in protected and record.pgid is not None
        }
        identity = target.identity.as_dict() if target is not None else None
        return {
            "available": True,
            "identity": identity,
            "pid": target.pid if target is not None else job_pid,
            "tty": tty,
            "foreground": target.foreground if target is not None else None,
            "active_jobs": [record.as_dict() for record in active],
            "stopped_jobs": [record.as_dict() for record in stopped],
            "protected_pids": sorted(protected),
            "protected": bool(
                target
                and (target.pid in protected or target.pgid in protected_groups)
            ),
            "protected_groups": sorted(protected_groups),
            "processes": [record.as_dict() for record in candidates],
        }

    def _record_for_identity(
        self,
        identity: Mapping[str, Any],
        records: Iterable[ProcessRecord],
        *,
        require_tty: bool = True,
    ) -> ProcessRecord | None:
        pid = _positive_pid(identity.get("pid"))
        start = identity.get(
            "start_time",
            identity.get(
                "start",
                identity.get(
                    "starttime",
                    identity.get("startTime", identity.get("process_start")),
                ),
            ),
        )
        if pid is None or start in (None, ""):
            return None
        for record in records:
            if record.pid == pid and str(record.start_time) == str(start):
                tty = identity.get("tty")
                if not require_tty or tty is None or record.tty == tty:
                    return record
        return None

    def interrupt_foreground(
        self,
        identity: Mapping[str, Any],
        row: Mapping[str, Any] | None = None,
        probe: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send SIGINT to one verified foreground process group.

        The PID and ``lstart`` value are checked again immediately before the
        signal.  Only the current foreground process group is targeted; there
        is no name-based fallback and no SIGKILL escalation.
        """

        if not self.enabled:
            raise RuntimeError("process interruption is unavailable")
        identity_map = dict(identity) if isinstance(identity, Mapping) else {}
        records = self.list_processes()
        if not records:
            # ``ps`` failure and a disappeared process both produce an empty
            # table.  A kill(0) probe distinguishes them conservatively: an
            # alive or permission-blocked PID is not an interrupt proof.
            pid = _positive_pid(identity_map.get("pid"))
            if pid is None:
                raise RuntimeError("process identity is unavailable")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                raise RuntimeError("process identity changed before interruption") from None
            except PermissionError:
                raise RuntimeError("process identity cannot be verified") from None
            except OSError:
                raise RuntimeError("process identity cannot be verified") from None
            raise RuntimeError("process table is unavailable")
        record = self._record_for_identity(identity_map, records)
        if record is None:
            raise RuntimeError("process identity changed before interruption")
        protected = self.protected_pids(records)
        protected_groups = {
            item.pgid
            for item in records
            if item.pid in protected and item.pgid is not None
        }
        if record.pid in protected or (
            record.pgid is not None and record.pgid in protected_groups
        ):
            raise PermissionError("target process is protected")
        if record.foreground is not True or record.pgid is None or record.pgid <= 0:
            raise RuntimeError("target process is not the foreground job")
        # os.kill accepts a negative process group ID and is easy to inject in
        # tests.  The group was proven to be foreground above.
        self._signaler(-record.pgid, signal.SIGINT)
        return {"sent": "SIGINT", "pid": record.pid, "pgid": record.pgid}

    def verify_interrupt(
        self,
        identity: Mapping[str, Any],
        row: Mapping[str, Any] | None = None,
        probe: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Boundedly verify the targeted process tree no longer owns the job slot.

        The foreground PID is not the whole proof: a child can survive a
        SIGINT after being reparented or detaching from the shell.  Every
        stable active/stopped identity captured in the original probe is
        checked independently before completion is reported.
        """

        observed: list[Mapping[str, Any]] = []
        if isinstance(identity, Mapping):
            observed.append(identity)
        if isinstance(probe, Mapping):
            process = probe.get("process")
            if isinstance(process, Mapping):
                for key in ("active_jobs", "stopped_jobs"):
                    values = process.get(key)
                    if isinstance(values, Iterable) and not isinstance(values, (str, bytes, Mapping)):
                        for item in values:
                            if not isinstance(item, Mapping):
                                continue
                            candidate = item.get("identity")
                            if isinstance(candidate, Mapping):
                                observed.append(candidate)
                            else:
                                observed.append(item)
        deduped: list[Mapping[str, Any]] = []
        seen: set[tuple[int, str, str | None]] = set()
        for candidate in observed:
            pid = _positive_pid(candidate.get("pid"))
            start = candidate.get(
                "start_time",
                candidate.get(
                    "start",
                    candidate.get(
                        "starttime",
                        candidate.get("startTime", candidate.get("process_start")),
                    ),
                ),
            )
            if pid is None or start in (None, ""):
                continue
            key = (pid, str(start), _clean_text(candidate.get("tty")))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        if not deduped:
            return {"verified": False, "complete": False}

        deadline = time.monotonic() + self.verify_timeout
        while True:
            records = self.list_processes()
            if not records:
                pids = [_positive_pid(candidate.get("pid")) for candidate in deduped]
                pids = [pid for pid in pids if pid is not None]
                if not pids:
                    return {"verified": False, "complete": False}
                for pid in pids:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue
                    except (PermissionError, OSError):
                        return {"verified": False, "complete": False}
                    return {"verified": False, "complete": False}
                return {"verified": True, "complete": True}
            current = [
                self._record_for_identity(candidate, records, require_tty=False)
                for candidate in deduped
            ]
            if all(item is None for item in current):
                return {"verified": True, "complete": True}
            if time.monotonic() >= deadline:
                return {"verified": False, "complete": False}
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def __call__(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.inspect(row)


# A provider named after its implementation is useful in code, while the
# shorter alias makes injection obvious in tests and adapters.
SubprocessProcessProvider = MacOSProcessProvider


def _nested_value(value: Any, name: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    item = value.get(name)
    if isinstance(item, Mapping):
        return item.get("value")
    return item


def _row_tty(row: Mapping[str, Any]) -> str | None:
    value = row.get("tty")
    if value is None:
        value = _nested_value(row.get("context"), "tty")
    return _clean_text(value)


__all__ = [
    "PS_FIELDS",
    "MacOSProcessProvider",
    "ProcessIdentity",
    "ProcessRecord",
    "SubprocessProcessProvider",
]
