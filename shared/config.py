# Ports for the local services
SUPPLIER_A_PORT = 8001
SUPPLIER_B_PORT = 8002
SUPPLIER_C_PORT = 8003
MONITOR_PORT = 8004

HOST = "localhost"

SUPPLIER_URLS = {
    "A": f"http://{HOST}:{SUPPLIER_A_PORT}",
    "B": f"http://{HOST}:{SUPPLIER_B_PORT}",
    "C": f"http://{HOST}:{SUPPLIER_C_PORT}",
}
MONITOR_URL = f"http://{HOST}:{MONITOR_PORT}"

# The browser may only talk to these origins (enforced by security/firewall.py)
ALLOWED_ORIGINS = [
    f"http://localhost:{p}" for p in (SUPPLIER_A_PORT, SUPPLIER_B_PORT, SUPPLIER_C_PORT)
] + [
    f"http://127.0.0.1:{p}" for p in (SUPPLIER_A_PORT, SUPPLIER_B_PORT, SUPPLIER_C_PORT)
]

# Default demo values
DEFAULT_MAX_BUDGET = 5000.0
MAX_RETRIES = 3