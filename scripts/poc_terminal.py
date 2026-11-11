"""Optional development terminal consumer of Xorchestrate's TerminalHost port.

The Rust application does not import this module. Xorchestrate owns terminal
mechanics and identity checks; this module retains a private selection and receipt.
Copyright 2026 xor.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import uuid

NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
MAX_RECORD = 1024 * 1024


class TerminalFailure(Exception):
    def __init__(self, message, code="terminal_consumer_error", **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _error(error):
    code = getattr(error, "code", "terminal_consumer_error")
    status = "error"
    if isinstance(error, ImportError):
        status, code = "unsupported", "xorchestrate_unavailable"
    elif code == "terminal_operation_unsupported":
        status = "unsupported"
    elif code in {"terminal_not_recoverable", "terminal_identity_mismatch", "session_unresolved"}:
        status = "unknown"
    details = getattr(error, "details", {})
    return {
        **(details if isinstance(details, dict) else {}),
        "status": status,
        "code": code,
        "message": str(error)[:1200],
    }


def _public(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as error:
            return _error(error)
    return guarded


def _api():
    try:
        from xorchestrate.instance.home import Home
        from xorchestrate.instance.services import Services, construct
        from xorchestrate.ports import TerminalCommand
    except ImportError as error:
        raise ImportError(
            "The pinned Xorchestrate terminal integration is unavailable. "
            "Run the PoC setup with host support and use its Python environment."
        ) from error
    return Home, Services, construct, TerminalCommand


def _root(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise TerminalFailure("The project root must be an existing directory.", "invalid_arguments")
    return root


def _name(name):
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise TerminalFailure("Use a terminal name of 1-64 letters, digits, dots, dashes or underscores.",
                              "invalid_arguments")
    return name


def _directory(path, *, private=True):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
            or info.st_uid != os.getuid() or info.st_mode & forbidden):
        raise TerminalFailure("The terminal state directory must be owned and private.",
                              "unsafe_terminal_state", path=str(path))
    return path


def _state(root):
    _directory(root / ".poc", private=False)
    return _directory(root / ".poc" / "terminal")


def _open_private(path, flags, mode=0o600):
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        os.close(descriptor)
        raise TerminalFailure("A terminal state file is not a private regular file.",
                              "unsafe_terminal_state", path=str(path))
    return descriptor


@contextmanager
def _lock(root):
    import fcntl
    state = _state(root)
    descriptor = _open_private(state / "control.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield state
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _record_path(state, name):
    return state / (_name(name) + ".json")


def _load(root, state, name):
    path = _record_path(state, name)
    try:
        descriptor = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        content = stream.read(MAX_RECORD + 1)
    if len(content) > MAX_RECORD:
        raise TerminalFailure("The terminal receipt is too large.", "invalid_terminal_receipt")
    record = json.loads(content)
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or record.get("project") != str(root) or record.get("name") != name
            or not isinstance(record.get("receipt"), dict)
            or not isinstance(record.get("selection"), dict)
            or not isinstance(record.get("run_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", record["run_id"]) is None):
        raise TerminalFailure("The terminal receipt does not belong to this project and name.",
                              "invalid_terminal_receipt")
    log = Path(record.get("log", ""))
    if not log.is_absolute() or log.parent != state / "logs":
        raise TerminalFailure("The terminal receipt has an invalid log location.", "invalid_terminal_receipt")
    return record


def _placement(root):
    key = hashlib.sha256(os.fsencode(root)).hexdigest()[:10]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime and Path(runtime).is_absolute() else Path("/tmp")
    try:
        info = base.lstat()
        if (base.is_symlink() or info.st_uid != os.getuid()
                or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or len(os.fsencode(base)) > 48):
            base = Path("/tmp")
    except OSError:
        base = Path("/tmp")
    location = Path(tempfile.mkdtemp(prefix="gcp-" + key + "-", dir=base))
    return {
        "name": "gcp-" + key + "-" + uuid.uuid4().hex[:10],
        "location": str(location),
        "socket": str(location / "host.sock"),
    }


def _remove_empty_placement(root, selection):
    options = selection.get("options", {})
    location = Path(options.get("location", ""))
    socket = Path(options.get("socket", ""))
    prefix = "gcp-" + hashlib.sha256(os.fsencode(root)).hexdigest()[:10] + "-"
    if not location.is_absolute() or not location.name.startswith(prefix) or socket.parent != location:
        return
    try:
        info = location.lstat()
        if location.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return
        children = location / "terminals"
        if children.is_dir() and not children.is_symlink():
            try:
                children.rmdir()
            except OSError:
                pass
        location.rmdir()  # Only empty directories; the port owns any socket.
    except OSError:
        pass


def _selected(root):
    Home, Services, _, _ = _api()
    settings = Home.locate().settings()
    # Resolve from one frozen read, including a fallback with different options.
    services = Services(SimpleNamespace(settings=lambda: deepcopy(settings)))
    placement = _placement(root)
    try:
        active = services.terminal_host(placement)
        primary, fallback = services.terminal_host_setting()
        specification = fallback if active.fallback_reason is not None else primary
        selection = {
            "adapter": active.adapter,
            "options": {**specification.get("options", {}), **placement},
        }
        return active, selection
    except BaseException:
        _remove_empty_placement(root, {"options": placement})
        raise


def _restore(record):
    _, _, construct, _ = _api()
    host = construct("terminal_host", deepcopy(record["selection"]), "graph-contracts terminal receipt")
    if not host.survives_controller:
        host.close()
        raise TerminalFailure("This saved terminal does not support a new controller.",
                              "terminal_not_recoverable")
    return host


def _observation(terminal):
    return {
        "running": terminal.running,
        "exit_status": terminal.exit_status,
        "exit_signal": terminal.exit_signal,
    }


def _summary(record):
    return {
        "name": record["name"],
        "run_id": record["run_id"],
        "project": record["project"],
        "log": record["log"],
        "host": record["host"],
        "created_at": record["created_at"],
    }


def _archive(state, record):
    history = _directory(state / "history")
    path = history / (record["name"] + "-" + record["run_id"] + ".json")
    if not path.exists():
        _write(path, record)


def _require_record(root, state, name):
    record = _load(root, state, name)
    if record is None:
        raise TerminalFailure("No terminal has been started under that name.", "terminal_not_started")
    return record


def _retained_output(record, lines):
    descriptor = _open_private(Path(record["log"]), os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - MAX_RECORD))
        text = stream.read(MAX_RECORD).decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


@_public
def doctor(root: Path) -> dict:
    root = _root(root)
    active, selection = _selected(root)
    try:
        return {
            "status": "ready" if active.host.survives_controller else "unsupported",
            "host": active.report(),
            "interactive_work": active.host.interactive and active.host.survives_controller,
            "project": str(root),
            "message": ("Terminal support is ready." if active.host.survives_controller
                        else "Owned hosts cannot outlive a detached launcher."),
        }
    finally:
        active.host.close()
        _remove_empty_placement(root, selection)


@_public
def start(root: Path, argv: list[str] | None = None, name="work") -> dict:
    root, name = _root(root), _name(name)
    interactive = argv is None
    command = ["/bin/bash", "--noprofile", "--norc", "-i"] if interactive else argv
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        raise TerminalFailure("The command must be a non-empty argv list.", "invalid_arguments")
    with _lock(root) as state:
        previous = _load(root, state, name)
        if previous is not None and previous.get("phase") != "closed":
            old_host = _restore(previous)
            try:
                try:
                    observed = old_host.adopt(previous["receipt"])
                except Exception as error:
                    raise TerminalFailure(
                        "The existing terminal cannot be reconciled. Inspect its retained log; "
                        "use a fresh name for a separate terminal.",
                        "session_unresolved", cause=getattr(error, "code", type(error).__name__),
                        **_summary(previous),
                    ) from error
                if observed.running:
                    raise TerminalFailure("A terminal with that name is already running.", "terminal_exists",
                                          **_summary(previous))
                closure = old_host.close_terminal(name)
                previous["last_observation"] = _observation(closure.terminal)
                previous["cleanup_verified"] = closure.cleanup_verified
                previous["phase"] = "closed" if closure.cleanup_verified else "cleanup_incomplete"
                _write(_record_path(state, name), previous)
                if not closure.cleanup_verified:
                    raise TerminalFailure("Cleanup of the earlier terminal is not verified.",
                                          "session_unresolved", **_summary(previous))
            finally:
                old_host.close()
            _remove_empty_placement(root, previous["selection"])
        active, selection = _selected(root)
        host, opened = active.host, None
        run_id = uuid.uuid4().hex
        log = _directory(state / "logs") / (name + "-" + run_id + ".log")
        try:
            if not host.survives_controller:
                raise TerminalFailure("The selected host cannot outlive this launcher.",
                                      "terminal_operation_unsupported", host=active.report())
            if interactive and not host.interactive:
                raise TerminalFailure(
                    "The selected host is not interactive. Configure an interactive terminal host for the work terminal.",
                    "terminal_operation_unsupported", host=active.report(),
                )
            _, _, _, TerminalCommand = _api()
            environment = dict(os.environ)
            environment["PATH"] = os.pathsep.join([
                str(root / "target" / "debug"), str(root / ".poc" / "bin"), environment.get("PATH", "")
            ])
            environment["GCP_POC_ROOT"] = str(root)
            opened = host.open_terminal(
                name,
                TerminalCommand(tuple(command), str(root), tuple(sorted(environment.items()))),
                log=str(log),
            )
            record = {
                "schema_version": 1, "project": str(root), "name": name, "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "phase": "active", "selection": selection, "host": active.report(),
                "receipt": deepcopy(opened.receipt), "log": str(log), "command": command,
                "last_observation": _observation(opened), "cleanup_verified": None,
            }
            if previous is not None:
                _archive(state, previous)
            _write(_record_path(state, name), record)
            return {"status": "started", **_summary(record), **_observation(opened)}
        except BaseException:
            try:
                if opened is not None or any(item.name == name for item in host.terminals()):
                    host.close_terminal(name)
            finally:
                _remove_empty_placement(root, selection)
            raise
        finally:
            host.close()


@_public
def status(root: Path, name="work") -> dict:
    root, name = _root(root), _name(name)
    with _lock(root) as state:
        record = _load(root, state, name)
        if record is None:
            return {"status": "not_started", "name": name, "running": None}
        if record.get("phase") == "closed":
            return {"status": "stopped", **_summary(record),
                    **record["last_observation"], "cleanup_verified": record["cleanup_verified"]}
        host = _restore(record)
        try:
            try:
                terminal = host.adopt(record["receipt"])
            except Exception as error:
                return {"status": "unknown", **_summary(record), "running": None,
                        "cleanup_verified": False, "observation_error": _error(error)}
            record["last_observation"] = _observation(terminal)
            _write(_record_path(state, name), record)
            return {"status": "running" if terminal.running else "exited",
                    **_summary(record), **_observation(terminal)}
        finally:
            host.close()


@_public
def output(root: Path, name="work", lines=100) -> dict:
    root, name = _root(root), _name(name)
    if type(lines) is not int or not 1 <= lines <= 2000:
        raise TerminalFailure("Output lines must be an integer from 1 to 2000.", "invalid_arguments")
    with _lock(root) as state:
        record = _require_record(root, state, name)
        if record.get("phase") == "closed":
            return {"status": "stopped", **_summary(record), "basis": "retained_log",
                    "output": _retained_output(record, lines)}
        host = _restore(record)
        try:
            try:
                terminal = host.adopt(record["receipt"])
            except Exception as error:
                return {"status": "unknown", **_summary(record), "basis": "retained_log",
                        "output": _retained_output(record, lines), "observation_error": _error(error)}
            return {"status": "running" if terminal.running else "exited", **_summary(record),
                    "basis": "terminal_host", "output": host.recent_output(name, lines=lines)}
        finally:
            host.close()


@_public
def input_text(root: Path, text: str, name="work") -> dict:
    """Send literal input through the host; useful for interactive consumer checks."""
    root, name = _root(root), _name(name)
    if not isinstance(text, str):
        raise TerminalFailure("Terminal input must be text.", "invalid_arguments")
    with _lock(root) as state:
        record = _require_record(root, state, name)
        host = _restore(record)
        try:
            host.adopt(record["receipt"])
            host.send_input(name, text)
            return {"status": "input_sent", **_summary(record)}
        finally:
            host.close()


@_public
def attach(root: Path, name="work", execute=False) -> dict:
    root, name = _root(root), _name(name)
    with _lock(root) as state:
        record = _require_record(root, state, name)
        host = _restore(record)
        try:
            instruction = host.attach_instruction_for(record["receipt"])
            answer = {"status": "instruction", **_summary(record),
                      "argv": list(instruction.argv), "message": instruction.text}
        finally:
            host.close()
    # A viewer must not hold the control lock: another command can stop the work.
    if execute:
        result = subprocess.run(answer["argv"], cwd=root, check=False)
        return {**answer, "status": "viewer_finished", "exit_code": result.returncode,
                "meaning": "viewer_exit_only"}
    return answer


@_public
def stop(root: Path, name="work") -> dict:
    root, name = _root(root), _name(name)
    with _lock(root) as state:
        record = _load(root, state, name)
        if record is None:
            return {"status": "not_started", "name": name, "cleanup_verified": None}
        if record.get("phase") == "closed":
            return {"status": "stopped", **_summary(record), "already_stopped": True,
                    "cleanup_verified": record["cleanup_verified"]}
        host = _restore(record)
        try:
            try:
                host.adopt(record["receipt"])
            except Exception as error:
                return {"status": "unknown", **_summary(record), "cleanup_verified": False,
                        "observation_error": _error(error)}
            closure = host.close_terminal(name)
            record["last_observation"] = _observation(closure.terminal)
            record["cleanup_verified"] = closure.cleanup_verified
            record["phase"] = "closed" if closure.cleanup_verified else "cleanup_incomplete"
            record["closed_at"] = datetime.now(timezone.utc).isoformat()
            _write(_record_path(state, name), record)
            answer = {"status": "stopped" if closure.cleanup_verified else "unknown",
                      **_summary(record), **_observation(closure.terminal),
                      "cleanup_verified": closure.cleanup_verified}
        finally:
            host.close()
        if closure.cleanup_verified:
            _remove_empty_placement(root, record["selection"])
        return answer
