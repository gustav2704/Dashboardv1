"""Authenticated SSH/SFTP transport for remote MT5 bridge folders.

This module transports DashboardBridge request/response files only.  It never
opens, changes, or closes trades.  Remote commands are limited to diagnostics
defined below; the public API deliberately does not accept arbitrary commands.
"""
from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import time
from contextlib import contextmanager
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any, Iterator


class SSHBridgeError(RuntimeError):
    pass


def _sftp_path(path: str) -> str:
    """Convert a Windows remote path to the form accepted by Win32 OpenSSH."""
    return PureWindowsPath(path).as_posix()


def _fingerprint(key: Any) -> str:
    value = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    return f"SHA256:{value}"


def _load_private_key(paramiko: Any, path: str) -> Any:
    # Paramiko 4 removed the legacy DSSKey class.  Build the list defensively so
    # modern installations can still load Ed25519/RSA/ECDSA keys.
    key_classes = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey)
    legacy_dss = getattr(paramiko, "DSSKey", None)
    if legacy_dss is not None:
        key_classes += (legacy_dss,)
    for key_class in key_classes:
        try:
            return key_class.from_private_key_file(path)
        except Exception:
            continue
    raise SSHBridgeError("The configured SSH private key could not be loaded")


class SSHBridge:
    def __init__(self, terminal: Any):
        self.host = str(terminal["ssh_host"] or "").strip()
        self.port = int(terminal["ssh_port"] or 22)
        self.username = str(terminal["ssh_username"] or "").strip()
        self.key_path = str(terminal["ssh_key_path"] or "").strip()
        self.host_fingerprint = str(terminal["ssh_host_fingerprint"] or "").strip()
        self.remote_data_dir = str(terminal["remote_data_dir"] or "").strip()
        if not all((self.host, self.username, self.key_path, self.host_fingerprint, self.remote_data_dir)):
            raise SSHBridgeError("Remote terminal SSH settings are incomplete")

    @contextmanager
    def _connection(self) -> Iterator[tuple[Any, Any]]:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - installation configuration
            raise SSHBridgeError("paramiko is required for SSH terminals") from exc
        transport = paramiko.Transport((self.host, self.port))
        try:
            transport.connect(username=self.username, pkey=_load_private_key(paramiko, self.key_path))
            actual = _fingerprint(transport.get_remote_server_key())
            if actual != self.host_fingerprint:
                raise SSHBridgeError("SSH host-key fingerprint does not match the registered terminal")
            client = paramiko.SFTPClient.from_transport(transport)
            try:
                yield transport, client
            finally:
                client.close()
        except SSHBridgeError:
            raise
        except Exception as exc:
            raise SSHBridgeError(f"SSH transport failed: {type(exc).__name__}: {exc}") from exc
        finally:
            transport.close()

    @contextmanager
    def _client(self) -> Iterator[Any]:
        with self._connection() as (_, client):
            yield client

    def execute(
        self,
        command: str,
        *,
        timeout: int = 300,
        cancel_event: Any | None = None,
        cancel_command: str | None = None,
    ) -> dict[str, Any]:
        """Execute an internal command over the pinned SSH connection.

        This primitive is intentionally not exposed through the HTTP API.  Its
        callers build fixed commands for the MT5 inventory/backtest workflow.
        """
        started = time.monotonic()
        with self._connection() as (transport, _):
            channel = transport.open_session()
            channel.exec_command(command)
            while not channel.exit_status_ready():
                if cancel_event is not None and cancel_event.is_set():
                    channel.close()
                    if cancel_command:
                        self.execute(cancel_command, timeout=30)
                    raise SSHBridgeError("Remote command was cancelled")
                if time.monotonic() - started > timeout:
                    channel.close()
                    raise SSHBridgeError(f"Remote command exceeded {timeout} seconds")
                time.sleep(0.2)
            output = channel.makefile("rb", -1).read().decode("utf-8", errors="replace")
            error = channel.makefile_stderr("rb", -1).read().decode("utf-8", errors="replace")
            status = channel.recv_exit_status()
        return {"status": status, "output": output, "error": error}

    def read_bytes_path(self, remote_path: str) -> bytes:
        with self._client() as sftp:
            with sftp.open(_sftp_path(remote_path), "rb") as handle:
                return handle.read()

    def read_many_paths(self, remote_paths: list[str]) -> dict[str, bytes]:
        """Read a bounded inventory set over one authenticated SFTP session."""
        result: dict[str, bytes] = {}
        with self._client() as sftp:
            for remote_path in remote_paths:
                with sftp.open(_sftp_path(remote_path), "rb") as handle:
                    result[remote_path] = handle.read()
        return result

    def write_bytes_path(self, remote_path: str, payload: bytes) -> None:
        """Write an isolated-runner artifact, replacing the exact target.

        Win32 OpenSSH's SFTP rename is not reliably replace-capable for EX5
        files, which made retrying an already-staged expert fail before MT5
        even started.  The caller writes only unique job files or a
        content-addressed EX5 staging path while no tester process is active,
        so an in-place overwrite is both safe and more reliable here.
        """
        destination = _sftp_path(remote_path)
        with self._client() as sftp:
            self._mkdirs(sftp, posixpath.dirname(destination))
            with sftp.open(destination, "wb") as handle:
                handle.write(payload)

    @staticmethod
    def _mkdirs(sftp: Any, directory: str) -> None:
        current = ""
        for part in directory.replace("\\", "/").split("/"):
            if not part:
                continue
            current = f"{current}/{part}" if current else part
            try:
                sftp.mkdir(current)
            except IOError:
                pass

    def download_path(self, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with self._client() as sftp:
            sftp.get(_sftp_path(remote_path), str(local_path))

    def list_files(self, remote_root: str, suffixes: tuple[str, ...] = ()) -> list[str]:
        """Recursively list regular files without following remote links."""
        result: list[str] = []
        pending = [_sftp_path(remote_root).rstrip("/")]
        with self._client() as sftp:
            while pending:
                directory = pending.pop()
                try:
                    entries = sftp.listdir_attr(directory)
                except IOError:
                    continue
                for entry in entries:
                    path = posixpath.join(directory, entry.filename)
                    mode = int(getattr(entry, "st_mode", 0))
                    if mode & 0o170000 == 0o040000:
                        pending.append(path)
                    elif not suffixes or entry.filename.casefold().endswith(suffixes):
                        result.append(str(PureWindowsPath(path)))
        return sorted(result)

    def bridge_path(self, *parts: str) -> str:
        return _sftp_path(str(PureWindowsPath(self.remote_data_dir, "MQL5", "Files", "Dashboardv1", *parts)))

    def read_text(self, *parts: str) -> str | None:
        path = self.bridge_path(*parts)
        with self._client() as sftp:
            try:
                with sftp.open(path, "r") as handle:
                    return handle.read().decode("utf-8-sig")
            except IOError:
                return None

    def write_json_atomic(self, payload: dict[str, Any], *parts: str) -> None:
        destination = self.bridge_path(*parts)
        directory = posixpath.dirname(destination)
        temporary = f"{destination}.dashboard-{int(time.time() * 1000)}.tmp"
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._client() as sftp:
            try:
                sftp.mkdir(directory)
            except IOError:
                pass
            with sftp.open(temporary, "wb") as handle:
                handle.write(encoded)
            sftp.rename(temporary, destination)

    def archive_response(self, kind: str) -> None:
        source = self.bridge_path("Responses", f"{kind}.response.json")
        archive = self.bridge_path("Responses", "Archive")
        destination = posixpath.join(archive, f"{kind}-{int(time.time() * 1000)}.json")
        with self._client() as sftp:
            try:
                sftp.mkdir(archive)
            except IOError:
                pass
            sftp.rename(source, destination)

    def diagnostic(self, name: str) -> dict[str, str]:
        commands = {
            "bridge_status": "powershell.exe -NoProfile -NonInteractive -Command \"Get-Service sshd | Select-Object Status,Name; Get-ChildItem -Force 'C:\\Users\\Administrator\\AppData\\Roaming\\MetaQuotes\\Terminal' | Select-Object Name,LastWriteTime\"",
        }
        if name not in commands:
            raise ValueError("Unsupported remote diagnostic")
        try:
            import paramiko
            transport = paramiko.Transport((self.host, self.port))
            transport.connect(username=self.username, pkey=_load_private_key(paramiko, self.key_path))
            actual = _fingerprint(transport.get_remote_server_key())
            if actual != self.host_fingerprint:
                raise SSHBridgeError("SSH host-key fingerprint does not match the registered terminal")
            channel = transport.open_session()
            channel.exec_command(commands[name])
            output = channel.makefile("r", -1).read()
            error = channel.makefile_stderr("r", -1).read()
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            if isinstance(error, bytes):
                error = error.decode("utf-8", errors="replace")
            status = channel.recv_exit_status()
            return {"name": name, "status": "ok" if status == 0 else "failed", "output": output, "error": error}
        except SSHBridgeError:
            raise
        except Exception as exc:
            raise SSHBridgeError(f"SSH diagnostic failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if "transport" in locals():
                transport.close()
