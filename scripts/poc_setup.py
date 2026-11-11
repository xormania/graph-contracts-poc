#!/usr/bin/env python3
"""Prepare the local PoC tools; optional host support stays outside the Rust crate."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

CUE_VERSION = "v0.17.1"
CUE_URL = "https://github.com/cue-lang/cue/releases/download/v0.17.1/cue_v0.17.1_linux_amd64.tar.gz"
CUE_SHA256 = "a39b0c97695069d95d276d99be0f5dbabb081d801bfdc9ba49b76efaf94e2369"
MAX_DOWNLOAD = 80 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def host_python(root: Path) -> Path:
    """Do not resolve this interpreter's symlink: its venv location matters."""
    return root.resolve() / ".poc" / "host" / "source" / ".venv" / "bin" / "python"


def _directory(path: Path) -> Path:
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlink at managed setup directory {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _write_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlink at setup receipt {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _require(name: str, guidance: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"Missing {name}. {guidance}")
    return str(Path(found).absolute())


class _Commands:
    def __init__(self, root: Path, logs: Path):
        self.root, self.logs, self.index = root, logs, 0

    def run(self, argv: list[str], *, cwd: Path | None = None,
            timeout: float = 60, env: dict | None = None) -> str:
        self.index += 1
        log = self.logs / f"{self.index:02d}-{Path(argv[0]).name}.log"
        print(f"setup: {Path(argv[0]).name} {' '.join(argv[1:3])}", file=sys.stderr)
        environment = dict(os.environ if env is None else env)
        for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            environment.pop(key, None)
        with log.open("xb") as stream:
            process = subprocess.Popen(
                argv, cwd=cwd or self.root, env=environment,
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
            try:
                status = process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait(timeout=5)
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                raise RuntimeError(f"Setup command interrupted or exceeded {timeout:g}s; see {log}") from None
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 8192))
            output = stream.read().decode("utf-8", errors="replace")
        if status:
            raise RuntimeError(f"Setup command exited {status}; see {log}\n{output.strip()}")
        return output.strip()


def _cue(root: Path, commands: _Commands) -> dict:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError(
            "Automatic CUE setup currently has a verified Linux amd64 archive only. "
            "Use setup --no-cue and configure an external CUE executable for other platforms."
        )
    state = root / ".poc"
    downloads = _directory(state / "downloads")
    archive = downloads / "cue-v0.17.1-linux-amd64.tar.gz"
    if archive.is_symlink():
        raise RuntimeError(f"Refusing a symlink at {archive}")
    if not archive.exists():
        print(f"setup: downloading CUE {CUE_VERSION}", file=sys.stderr)
        request = Request(CUE_URL, headers={"User-Agent": "graph-contracts-poc-setup"})
        deadline = time.monotonic() + 120
        data = bytearray()
        with urlopen(request, timeout=30) as response:
            while block := response.read(65536):
                data.extend(block)
                if len(data) > MAX_DOWNLOAD or time.monotonic() > deadline:
                    raise RuntimeError("CUE download exceeded its size or time bound.")
        if hashlib.sha256(data).hexdigest() != CUE_SHA256:
            raise RuntimeError("CUE archive checksum mismatch; no executable was installed.")
        descriptor, temporary = tempfile.mkstemp(prefix=".cue-download-", dir=downloads)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.replace(temporary, archive)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if digest(archive) != CUE_SHA256:
        raise RuntimeError(f"Cached CUE archive differs from the pin: {archive}. Preserve or remove it explicitly.")
    with tarfile.open(archive, "r:gz") as package:
        matches = [item for item in package.getmembers() if item.name in {"cue", "./cue"}]
        if len(matches) != 1 or not matches[0].isreg() or matches[0].size > MAX_DOWNLOAD:
            raise RuntimeError("Verified CUE archive has an unexpected executable entry.")
        stream = package.extractfile(matches[0])
        if stream is None:
            raise RuntimeError("CUE executable could not be read from the archive.")
        with stream:
            executable = stream.read(MAX_DOWNLOAD + 1)
        if len(executable) > MAX_DOWNLOAD:
            raise RuntimeError("CUE executable exceeds its size bound.")
    binary = _directory(state / "bin") / "cue"
    expected = hashlib.sha256(executable).hexdigest()
    if binary.is_symlink() or (binary.exists() and digest(binary) != expected):
        raise RuntimeError(f"Existing CUE executable differs from the pin: {binary}. It was not replaced.")
    if not binary.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".cue-", dir=binary.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(executable)
            Path(temporary).chmod(0o700)
            os.replace(temporary, binary)
        finally:
            Path(temporary).unlink(missing_ok=True)
    version = commands.run([str(binary), "version"], timeout=30).splitlines()[0]
    if version != f"cue version {CUE_VERSION}":
        raise RuntimeError(f"Unexpected CUE version: {version}")
    return {"path": str(binary), "version": version, "sha256": expected,
            "archive_sha256": CUE_SHA256, "source": CUE_URL}


def _host(root: Path, commands: _Commands, tools: dict) -> dict:
    import tomllib

    lock = json.loads((root / "poc" / "host-lock.json").read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1 or any(
        not isinstance(lock.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", lock[key])
        for key in ("revision", "xcontracts_revision")
    ):
        raise RuntimeError("poc/host-lock.json must identify complete host and xcontracts commit hashes.")
    base = _directory(root / ".poc" / "host")
    source = base / "source"
    if source.is_symlink():
        raise RuntimeError(f"Refusing a symlink at host checkout {source}")
    if not source.exists():
        # A failed fetch leaves no partial checkout at the public setup location.
        with tempfile.TemporaryDirectory(prefix=".source-", dir=base) as temporary:
            staging = Path(temporary) / "checkout"
            staging.mkdir()
            commands.run([tools["git"], "init", str(staging)], timeout=30)
            commands.run([tools["git"], "remote", "add", "origin", lock["repository"]], cwd=staging)
            commands.run([tools["git"], "fetch", "--depth", "1", "origin", lock["revision"]],
                         cwd=staging, timeout=180)
            commands.run([tools["git"], "checkout", "--detach", "FETCH_HEAD"], cwd=staging)
            staging.rename(source)
    revision = commands.run([tools["git"], "rev-parse", "HEAD"], cwd=source)
    remote = commands.run([tools["git"], "remote", "get-url", "origin"], cwd=source)
    changed = commands.run([tools["git"], "status", "--porcelain", "--untracked-files=normal"], cwd=source)
    if revision != lock["revision"] or remote != lock["repository"] or changed:
        raise RuntimeError(
            f"Host checkout is changed or differs from poc/host-lock.json: {source}. "
            "Setup will not reset it. Preserve it and move it aside before preparing a fresh pinned host."
        )
    if (source / ".venv").is_symlink():
        raise RuntimeError(f"Refusing a symlink at host environment {source / '.venv'}")
    configuration = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    declared = configuration["tool"]["uv"]["sources"]["xcontracts"]["rev"]
    if declared != lock["xcontracts_revision"]:
        raise RuntimeError("Host source selects a different xcontracts revision from poc/host-lock.json.")
    environment = dict(os.environ)
    for key in ("VIRTUAL_ENV", "UV_PROJECT", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(key, None)
    environment.update({
        "UV_PROJECT_ENVIRONMENT": str(source / ".venv"),
        "UV_CACHE_DIR": str(_directory(root / ".poc" / "cache") / "uv"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    commands.run(
        [tools["uv"], "sync", "--locked", "--no-dev", "--python", str(Path(sys.executable).resolve()),
         "--no-python-downloads"],
        cwd=source, env=environment, timeout=600,
    )
    python = host_python(root)
    versions = json.loads(commands.run(
        [str(python), "-c",
         "import json,sys,importlib.metadata as m; "
         "print(json.dumps({'python':sys.version.split()[0],"
         "'xorchestrate':m.version('xorchestrate'),'xcontracts':m.version('xcontracts')}))"],
        cwd=source, env=environment,
    ))
    return {**lock, "path": str(source), "python": str(python), "versions": versions,
            "uv_lock_sha256": digest(source / "uv.lock"),
            "uv_version": commands.run([tools["uv"], "--version"]),
            "tmux_available": tools["tmux"] is not None,
            "tmux_version": commands.run([tools["tmux"], "-V"]) if tools["tmux"] else None}


def setup(root: Path, with_terminal: bool = False, cue: bool = True) -> dict:
    root = root.resolve()
    cargo = _require("cargo", "Install Rust with the toolchain specified in rust-toolchain.toml.")
    rustc = _require("rustc", "Install the Rust toolchain specified in rust-toolchain.toml.")
    tools = {}
    if with_terminal:
        if sys.version_info < (3, 12):
            raise RuntimeError("Terminal setup requires Python 3.12 or newer; rerun with that interpreter.")
        if platform.system() != "Linux" or not Path("/proc").is_dir():
            raise RuntimeError("The pinned terminal host requires Linux or WSL2 with /proc and PTYs.")
        for name, guidance in (
            ("git", "Install git before preparing the pinned host."),
            ("uv", "Install uv before preparing the optional host environment."),
        ):
            tools[name] = _require(name, guidance)
        tmux = shutil.which("tmux")
        tools["tmux"] = str(Path(tmux).absolute()) if tmux else None
    state = _directory(root / ".poc")
    setup_lock = state / "setup.lock"
    try:
        descriptor = os.open(setup_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError(
            f"Setup lock exists: {setup_lock}. Check whether setup is running; "
            "remove a stale lock explicitly before retrying."
        ) from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(f"{os.getpid()}\n")
    receipt = {"schema_version": 1, "status": "configuring", "root": str(root),
               "started_at": datetime.now(timezone.utc).isoformat(),
               "selected": {"cue": cue, "terminal": with_terminal}}
    try:
        logs = _directory(state / "setup-logs")
        run_logs = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=logs))
        commands = _Commands(root, run_logs)
        receipt["logs"] = str(run_logs)
        _write_json(state / "setup.json", receipt)
        receipt["tools"] = {"cargo": commands.run([cargo, "--version"]),
                            "rustc": commands.run([rustc, "--version"])}
        commands.run([cargo, "build", "--locked", "--target-dir", str(root / "target")], timeout=600)
        binary = root / "target" / "debug" / ("gcp.exe" if os.name == "nt" else "gcp")
        receipt["gcp"] = {"path": str(binary), "sha256": digest(binary),
                          "version": commands.run([str(binary), "--version"]),
                          "cargo_lock_sha256": digest(root / "Cargo.lock")}
        if cue:
            receipt["cue"] = _cue(root, commands)
        if with_terminal:
            receipt["host"] = _host(root, commands, tools)
        receipt["status"] = "ready"
        return receipt
    except BaseException as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        raise
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _write_json(state / "setup.json", receipt)
        finally:
            setup_lock.unlink(missing_ok=True)
