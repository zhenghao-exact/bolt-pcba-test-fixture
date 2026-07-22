"""Label-printer send with automatic fallback to another Pi.

The Brother QL-800 lives on one bench at a time (it's hard to move; the scanner
moves freely). So printing tries the LOCAL ``/dev/usb/lp0`` first, and if there is
no local printer (or the local send fails), it forwards the job to another fixture
Pi that has one, over SSH.

This works because brother_ql's ``instructions`` (from ``convert()``) is a
self-contained raster byte payload — the ``linux_kernel`` backend just writes those
bytes to ``/dev/usb/lp0`` — so a remote print is simply piping the bytes to
``ssh <pi> 'cat > /dev/usb/lp0'``. No brother_ql is needed on the remote. The remote
print is fire-and-forget (the blocking status read-back is lost — acceptable for a
fallback).

Remote targets come from the env var ``REMOTE_PRINTER_HOSTS`` (comma-separated ssh
targets), else the default below. This bench is the Pi 4 PCBA station, so its
default fallback is the Pi 5 gateway bench that normally holds the printer.
"""
import os
import subprocess

LOCAL_PRINTER = "/dev/usb/lp0"
DEFAULT_REMOTE_HOSTS = ["exact-pi@192.168.1.7"]  # Pi 5 static LAN IP; override with REMOTE_PRINTER_HOSTS
# accept-new auto-trusts an unknown host key on first contact (Pi-to-Pi), while still
# refusing a *changed* key; BatchMode keeps it non-interactive.
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
             "-o", "StrictHostKeyChecking=accept-new"]


def remote_hosts():
    env = os.environ.get("REMOTE_PRINTER_HOSTS", "").strip()
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    return list(DEFAULT_REMOTE_HOSTS)


def _local_send(instructions) -> bool:
    from brother_ql.backends.helpers import send  # lazy: only needed for the local path
    send(instructions=instructions, printer_identifier=LOCAL_PRINTER,
         backend_identifier="linux_kernel", blocking=True)
    return True


def _remote_has_printer(host: str) -> bool:
    try:
        r = subprocess.run(["ssh", *_SSH_OPTS, host, "test -e /dev/usb/lp0"],
                           timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _remote_send(host: str, instructions) -> bool:
    # Write the raster bytes straight to the remote device; if that Pi's lp0 isn't
    # world-writable, retry via passwordless sudo tee.
    for remote_cmd in ("cat > /dev/usb/lp0", "sudo -n tee /dev/usb/lp0 > /dev/null"):
        try:
            r = subprocess.run(["ssh", *_SSH_OPTS, host, remote_cmd],
                               input=instructions, timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def send_label(instructions) -> bool:
    """Print ``instructions`` locally if a local printer exists, else forward to a
    remote Pi that has one. Returns True on success; prints its own diagnostics."""
    if os.path.exists(LOCAL_PRINTER):
        try:
            return _local_send(instructions)
        except Exception as e:
            print(f"Local print failed ({e}); trying a remote printer…")
    else:
        print(f"No local printer at {LOCAL_PRINTER}; looking for one on another Pi…")

    tried = []
    for host in remote_hosts():
        tried.append(host)
        if not _remote_has_printer(host):
            continue
        if _remote_send(host, instructions):
            print(f"Printed on remote Pi {host}.")
            return True
        print(f"Remote print to {host} failed.")
    print(f"No printer found locally or on any remote Pi (tried: {', '.join(tried) or 'none'}).")
    return False
