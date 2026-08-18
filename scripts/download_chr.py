import os, paramiko, sys

HOST = "145.63.135.196"
USER = "Administrator"
KEY = os.path.join(os.path.dirname(__file__), "..", "ssh_access", "codex_vps_ed25519")
TERM = "6C3C6A11D1C3791DD4DBF45421BF8028"
REMOTE_DIR = f"C:/Users/Administrator/AppData/Roaming/MetaQuotes/Terminal/{TERM}/MQL5/Profiles/Charts/Default"
LOCAL = os.path.join(os.path.dirname(__file__), "..", "data", "chr_probe")
os.makedirs(LOCAL, exist_ok=True)

t = paramiko.Transport((HOST, 22))
t.connect(username=USER, pkey=paramiko.Ed25519Key.from_private_key_file(os.path.abspath(KEY)))
sftp = paramiko.SFTPClient.from_transport(t)
files = [f for f in sftp.listdir(REMOTE_DIR) if f.lower().endswith(".chr")]
print("files:", files)
for f in files:
    rp = f"{REMOTE_DIR}/{f}"
    lp = os.path.join(LOCAL, f)
    sftp.get(rp, lp)
    print("downloaded", f, os.path.getsize(lp))
sftp.close(); t.close()
