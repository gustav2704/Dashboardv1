"""Reliable SSH/SFTP helper for the trading VPSes (Windows remote with PowerShell)."""
import base64
import os
import sys
import paramiko

HOSTS = {
    "145": {"host": "145.63.130.53", "user": "Administrator", "termroot": "C:/Users/Administrator/AppData/Roaming/MetaQuotes/Terminal"},
    "100": {"host": "100.107.77.50", "user": "Administrador", "termroot": "C:/Users/Administrador/AppData/Roaming/MetaQuotes/Terminal"},
}
KEY = os.path.join(os.path.dirname(__file__), "..", "ssh_access", "codex_vps_ed25519")


def _client(tag):
    info = HOSTS[tag]
    t = paramiko.Transport((info["host"], 22))
    t.connect(username=info["user"], pkey=paramiko.Ed25519Key.from_private_key_file(os.path.abspath(KEY)))
    return t, info


def run_ps(tag, script, timeout=120):
    """Run a PowerShell script on the remote and return its stdout text."""
    t, info = _client(tag)
    try:
        enc = base64.b64encode(script.encode("utf-16le")).decode()
        cmd = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + enc
        ch = t.open_session()
        ch.settimeout(timeout)
        ch.exec_command(cmd)
        out = b""
        while True:
            if ch.recv_ready():
                out += ch.recv(65536)
            if ch.exit_status_ready() and not ch.recv_ready():
                break
        status = ch.recv_exit_status()
        return status, out.decode("utf-8", "replace")
    finally:
        t.close()


def sftp_list(tag, remote_dir):
    t, info = _client(tag)
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        return sftp.listdir_attr(remote_dir)
    finally:
        t.close()


def sftp_download(tag, remote_path, local_path):
    t, info = _client(tag)
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        sftp.get(remote_path, local_path)
    finally:
        t.close()


def sftp_download_many(tag, remote_paths, local_dir):
    t, info = _client(tag)
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        os.makedirs(local_dir, exist_ok=True)
        result = {}
        for rp in remote_paths:
            lp = os.path.join(local_dir, os.path.basename(rp))
            try:
                sftp.get(rp, lp)
                result[rp] = lp
            except IOError as e:
                result[rp] = None
        return result
    finally:
        t.close()


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "145"
    script = sys.argv[2] if len(sys.argv) > 2 else "Get-Date"
    status, out = run_ps(tag, script)
    print(f"STATUS={status}")
    print(out)
