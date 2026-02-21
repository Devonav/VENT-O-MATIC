"""
Vend-O-Matic — HTTP vending machine API
stdlib only: http.server, json, threading, os
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Shared state
# All reads and writes go through _lock to stay thread-safe when
# ThreadingHTTPServer dispatches concurrent requests.
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_state = {
    "coins": 0,           # quarters inserted, not yet spent
    "inventory": [5, 5, 5],  # quantities for item IDs 0, 1, 2
}

ITEM_PRICE = 2  # quarters required per purchase


class VendingHandler(BaseHTTPRequestHandler):

    # ------------------------------------------------------------------ #
    # Dispatch — called by BaseHTTPRequestHandler for each HTTP method    #
    # ------------------------------------------------------------------ #

    def do_GET(self):
        segments, item_id = self._parse_path()
        if segments is None:
            return  # _parse_path already sent a 404
        if segments == ["inventory"]:
            self._handle_inventory_get()
        elif len(segments) == 2 and segments[0] == "inventory" and item_id is not None:
            self._handle_item_get(item_id)
        else:
            self._send_error_plain(404, {})

    def do_PUT(self):
        segments, item_id = self._parse_path()
        if segments is None:
            return
        if segments == [""]:
            self._handle_root_put()
        elif len(segments) == 2 and segments[0] == "inventory" and item_id is not None:
            self._handle_item_put(item_id)
        else:
            self._send_error_plain(404, {})

    def do_DELETE(self):
        segments, item_id = self._parse_path()
        if segments is None:
            return
        if segments == [""]:
            self._handle_root_delete()
        else:
            self._send_error_plain(404, {})

    # ------------------------------------------------------------------ #
    # Path parsing                                                         #
    # ------------------------------------------------------------------ #

    def _parse_path(self):
        """
        Normalize the request path and extract a validated item ID if present.

        Returns:
            (segments, item_id) on success — item_id is None for non-item routes.
            (None, None) if the path is invalid; response has already been sent.
        """
        # Strip query string and trailing slash; treat bare "/" specially
        raw = self.path.split("?")[0].rstrip("/") or "/"
        if raw == "/":
            return [""], None

        parts = raw.lstrip("/").split("/")

        item_id = None
        if len(parts) == 2 and parts[0] == "inventory":
            # Validate that the ID segment is a non-negative integer in range
            try:
                item_id = int(parts[1])
            except ValueError:
                self._send_error_plain(404, {})
                return None, None
            with _lock:
                if item_id < 0 or item_id >= len(_state["inventory"]):
                    self._send_error_plain(404, {})
                    return None, None

        return parts, item_id

    # ------------------------------------------------------------------ #
    # Route handlers                                                       #
    # ------------------------------------------------------------------ #

    def _handle_root_put(self):
        """PUT / — accept a single quarter and add it to the running total."""
        body = self._read_json_body()
        if body is None:
            self._send_error_plain(400, {})
            return

        coin = body.get("coin", 0)
        if coin not in (0, 1):  # machine only accepts one coin at a time
            self._send_error_plain(400, {})
            return

        with _lock:
            _state["coins"] += coin
            total = _state["coins"]

        # X-Coins reflects the total accepted so far in this session
        self._send_no_content({"X-Coins": str(total)})

    def _handle_root_delete(self):
        """DELETE / — cancel the transaction and return all inserted coins."""
        with _lock:
            returned = _state["coins"]
            _state["coins"] = 0
        self._send_no_content({"X-Coins": str(returned)})

    def _handle_inventory_get(self):
        """GET /inventory — return a snapshot of all item quantities."""
        with _lock:
            snapshot = list(_state["inventory"])  # copy to release lock quickly
        self._send_json(200, snapshot, {})

    def _handle_item_get(self, item_id):
        """GET /inventory/:id — return the quantity for a single item."""
        with _lock:
            qty = _state["inventory"][item_id]
        self._send_json(200, qty, {})

    def _handle_item_put(self, item_id):
        """
        PUT /inventory/:id — attempt to purchase the item.

        Priority of error checks (per spec):
          1. Out of stock  → 404, coins kept
          2. Insufficient funds → 403, coins kept
          3. Success → 200, change returned via X-Coins
        """
        with _lock:
            qty = _state["inventory"][item_id]
            coins = _state["coins"]

            # Check 1: out of stock — coins stay in machine
            if qty == 0:
                self._send_error_plain(404, {"X-Coins": str(coins)})
                return

            # Check 2: not enough quarters — coins stay in machine
            if coins < ITEM_PRICE:
                self._send_error_plain(403, {"X-Coins": str(coins)})
                return

            # Success: decrement inventory, deduct price, compute change
            _state["inventory"][item_id] -= 1
            _state["coins"] -= ITEM_PRICE
            new_qty = _state["inventory"][item_id]
            change = _state["coins"]  # leftover quarters returned to customer

        # quantity in the body = items dispensed this transaction (always 1)
        self._send_json(
            200,
            {"quantity": 1},
            {"X-Coins": str(change), "X-Inventory-Remaining": str(new_qty)},
        )

    # ------------------------------------------------------------------ #
    # Response helpers                                                     #
    # ------------------------------------------------------------------ #

    def _send_no_content(self, headers):
        """Send a 204 with custom headers and no body."""
        self.send_response(204)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

    def _send_json(self, code, body, headers):
        """Serialize body to JSON and send with correct Content-Type/Length."""
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_plain(self, code, headers):
        """
        Send an error status with optional headers and no body.
        Avoids BaseHTTPRequestHandler.send_error() which appends an HTML body.
        """
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

    def _read_json_body(self):
        """
        Read and parse the request body using Content-Length.
        Returns a dict on success, empty dict if no body, None on parse error.
        """
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------ #
    # Logging                                                              #
    # ------------------------------------------------------------------ #

    def log_message(self, fmt, *args):
        # Route access logs to stdout instead of stderr (default behavior)
        print(f"{self.address_string()} - {fmt % args}")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class _ReuseAddrServer(ThreadingHTTPServer):
    # SO_REUSEADDR must be set before bind(), so it lives as a class attribute.
    # This allows the server to restart immediately without "Address already in use".
    allow_reuse_address = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    server = _ReuseAddrServer(("", port), VendingHandler)
    print(f"Vend-O-Matic listening on port {port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
