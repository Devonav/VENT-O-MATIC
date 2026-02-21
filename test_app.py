"""
Tests for Vend-O-Matic API.
Starts the server in a background thread, runs all cases, then shuts down.
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

# Import the handler and reset state between tests
import app as vending


BASE = "http://localhost:18080"


def _request(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, dict(e.headers), raw


def _reset_state():
    with vending._lock:
        vending._state["coins"] = 0
        vending._state["inventory"] = [5, 5, 5]


class TestVendOMatic(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("", 18080), vending.VendingHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _reset_state()

    # ------------------------------------------------------------------ #
    # PUT /  — insert coin                                                 #
    # ------------------------------------------------------------------ #

    def test_insert_one_coin(self):
        status, headers, _ = _request("PUT", "/", {"coin": 1})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("X-Coins"), "1")

    def test_insert_two_coins(self):
        _request("PUT", "/", {"coin": 1})
        status, headers, _ = _request("PUT", "/", {"coin": 1})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("X-Coins"), "2")

    def test_insert_zero_coin_noop(self):
        status, headers, _ = _request("PUT", "/", {"coin": 0})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("X-Coins"), "0")

    # ------------------------------------------------------------------ #
    # DELETE /  — return coins                                             #
    # ------------------------------------------------------------------ #

    def test_cancel_returns_coins(self):
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/", {"coin": 1})
        status, headers, _ = _request("DELETE", "/")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("X-Coins"), "2")

    def test_cancel_with_no_coins(self):
        status, headers, _ = _request("DELETE", "/")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("X-Coins"), "0")

    def test_cancel_resets_coin_count(self):
        _request("PUT", "/", {"coin": 1})
        _request("DELETE", "/")
        status, headers, _ = _request("DELETE", "/")
        self.assertEqual(headers.get("X-Coins"), "0")

    # ------------------------------------------------------------------ #
    # GET /inventory                                                       #
    # ------------------------------------------------------------------ #

    def test_get_inventory_initial(self):
        status, _, body = _request("GET", "/inventory")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [5, 5, 5])

    def test_get_inventory_after_purchase(self):
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/inventory/1")
        _, _, body = _request("GET", "/inventory")
        self.assertEqual(json.loads(body), [5, 4, 5])

    # ------------------------------------------------------------------ #
    # GET /inventory/:id                                                   #
    # ------------------------------------------------------------------ #

    def test_get_item_quantity(self):
        status, _, body = _request("GET", "/inventory/0")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), 5)

    def test_get_item_invalid_id(self):
        status, _, _ = _request("GET", "/inventory/99")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------ #
    # PUT /inventory/:id  — purchase                                       #
    # ------------------------------------------------------------------ #

    def test_happy_path_purchase(self):
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/", {"coin": 1})
        status, headers, body = _request("PUT", "/inventory/0")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Coins"), "0")
        self.assertEqual(headers.get("X-Inventory-Remaining"), "4")
        self.assertEqual(json.loads(body), {"quantity": 1})

    def test_purchase_with_change(self):
        # Insert 3 coins, buy → 1 coin change
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/", {"coin": 1})
        status, headers, body = _request("PUT", "/inventory/2")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Coins"), "1")
        self.assertEqual(json.loads(body), {"quantity": 1})

    def test_insufficient_funds_zero_coins(self):
        status, headers, _ = _request("PUT", "/inventory/0")
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("X-Coins"), "0")

    def test_insufficient_funds_one_coin(self):
        _request("PUT", "/", {"coin": 1})
        status, headers, _ = _request("PUT", "/inventory/0")
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("X-Coins"), "1")

    def test_out_of_stock_returns_404(self):
        # Deplete item 0
        for _ in range(5):
            _request("PUT", "/", {"coin": 1})
            _request("PUT", "/", {"coin": 1})
            _request("PUT", "/inventory/0")
        # Insert coins and try again
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/", {"coin": 1})
        status, headers, _ = _request("PUT", "/inventory/0")
        self.assertEqual(status, 404)
        # Coins are kept (not returned)
        self.assertEqual(headers.get("X-Coins"), "2")

    def test_out_of_stock_checked_before_insufficient_funds(self):
        # Deplete item 1 with 0 coins in machine
        for _ in range(5):
            _request("PUT", "/", {"coin": 1})
            _request("PUT", "/", {"coin": 1})
            _request("PUT", "/inventory/1")
        # Now item 1 is empty; insert only 1 coin (insufficient funds too)
        _request("PUT", "/", {"coin": 1})
        status, _, _ = _request("PUT", "/inventory/1")
        # Out-of-stock (404) wins over insufficient funds (403)
        self.assertEqual(status, 404)

    def test_purchase_invalid_item_id(self):
        status, _, _ = _request("PUT", "/inventory/99")
        self.assertEqual(status, 404)

    def test_inventory_decrements_correctly(self):
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/", {"coin": 1})
        _request("PUT", "/inventory/2")
        status, _, body = _request("GET", "/inventory/2")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), 4)

    # ------------------------------------------------------------------ #
    # Unknown routes                                                       #
    # ------------------------------------------------------------------ #

    def test_unknown_get_route(self):
        status, _, _ = _request("GET", "/unknown")
        self.assertEqual(status, 404)

    def test_unknown_put_route(self):
        status, _, _ = _request("PUT", "/unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
