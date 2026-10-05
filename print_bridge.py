"""Print bridge: runs on the PC/Raspberry Pi next to the receipt printer.
Env vars: SERVER_URL, PRINT_API_KEY, PRINTER_MODE (network|device|console),
PRINTER_HOST, PRINTER_PORT, PRINTER_DEVICE, POLL_SECONDS, PRINT_CODEPAGE."""
import os, sys, json, time, socket
import urllib.request, urllib.error

SERVER = os.getenv("SERVER_URL", "http://localhost:5000").rstrip("/")
KEY = os.getenv("PRINT_API_KEY", "change-me-bridge-key")
MODE = os.getenv("PRINTER_MODE", "network")
HOST = os.getenv("PRINTER_HOST", "192.168.1.50")
PORT = int(os.getenv("PRINTER_PORT", "9100"))
DEVICE = os.getenv("PRINTER_DEVICE", "/dev/usb/lp0")
POLL = float(os.getenv("POLL_SECONDS", "3"))
CODEPAGE = os.getenv("PRINT_CODEPAGE", "cp437")


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(
        SERVER + path, data=data, method=method,
        headers={"X-Print-Key": KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def escpos(text):
    return b"\x1b@" + text.encode(CODEPAGE, "replace") + b"\n\n\n\n\x1dV\x42\x00"


def send(text):
    if MODE == "console":
        print(text, "\n" + "~" * 42)
        return
    payload = escpos(text)
    if MODE == "network":
        with socket.create_connection((HOST, PORT), timeout=10) as s:
            s.sendall(payload)
    elif MODE == "device":
        with open(DEVICE, "wb") as f:
            f.write(payload)
    else:
        raise RuntimeError(f"Unknown PRINTER_MODE '{MODE}'")


def main():
    print(f"Print bridge -> {SERVER} (mode={MODE})")
    while True:
        try:
            job = call("/api/print/next").get("job")
            if not job:
                time.sleep(POLL)
                continue
            try:
                send(job["text"])
                call(f"/api/print/{job['id']}/done", "POST", {})
                print(f"Printed job {job['id']}")
            except Exception as e:
                print(f"Job {job['id']} failed: {e}", file=sys.stderr)
                call(f"/api/print/{job['id']}/failed", "POST", {"error": str(e)[:300]})
                time.sleep(POLL)
        except (urllib.error.URLError, OSError) as e:
            print(f"Server unreachable: {e}", file=sys.stderr)
            time.sleep(POLL * 2)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()