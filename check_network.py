"""Safe TCP connectivity check: no Telegram credentials are read or sent."""
import socket

TARGETS = [("Telegram DC2", "149.154.167.51", 443),
           ("Telegram DC2 alternate", "149.154.167.50", 443),
           ("Telegram DC2 port 80", "149.154.167.51", 80)]
print("Checking TCP connectivity (this does not log into Telegram)...")
for label, host, port in TARGETS:
    try:
        with socket.create_connection((host, port), timeout=5):
            print(f"{label} {host}:{port}  OPEN")
    except OSError as exc:
        print(f"{label} {host}:{port}  FAILED ({type(exc).__name__})")
print("If all failed: switch networks (mobile hotspot), or check VPN/firewall.")
print("If some are OPEN but Telethon fails: try obfuscated connection mode.")
print("Public test API credentials may still reject login; use your own for real operation.")
