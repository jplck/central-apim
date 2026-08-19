"""Energy-provider customer-profile MCP server (demo).

Read-only. All data is fully defined in data.json (5 customers) — no DB, no auth.
Speaks MCP over Streamable HTTP so it can run behind Container Apps ingress.

Run:      python server.py            # serves MCP at http://0.0.0.0:8000/mcp
Selftest: python server.py selftest   # asserts the lookup/filter logic
"""
import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DATA = json.loads((Path(__file__).parent / "data.json").read_text())
CUSTOMERS = DATA["customers"]
# meter_id -> owning customer_id, built once at startup.
METER_OWNER = {m["meter_id"]: cid for cid, c in CUSTOMERS.items() for m in c["meters"]}

# stateless_http/json_response: the Agent 365 Tooling Gateway (and its discovery client)
# calls tools/list without threading an Mcp-Session-Id, which a stateful server rejects
# with HTTP 400. Stateless + JSON responses make each request self-contained.
mcp = FastMCP("energy-profile", host="0.0.0.0", port=8000,
              stateless_http=True, json_response=True)


def _customer(customer_id: str) -> dict:
    c = CUSTOMERS.get(customer_id)
    if c is None:
        raise ValueError(f"unknown customer_id: {customer_id}")
    return c


@mcp.tool()
def get_customer_profile(customer_id: str) -> dict:
    """Account holder, address, tariff and meter list for a customer."""
    return _customer(customer_id)["profile"]


@mcp.tool()
def list_meters(customer_id: str) -> list:
    """Meters (electricity/gas) registered to the customer."""
    return _customer(customer_id)["meters"]


@mcp.tool()
def get_meter_readings(meter_id: str, start: str = "", end: str = "") -> list:
    """Daily readings for one meter, optionally filtered by inclusive ISO date range."""
    cid = METER_OWNER.get(meter_id)
    if cid is None:
        raise ValueError(f"unknown meter_id: {meter_id}")
    rows = CUSTOMERS[cid]["readings"].get(meter_id, [])
    # ISO-8601 dates sort lexicographically, so string comparison is a valid range filter.
    return [r for r in rows
            if (not start or r["timestamp"] >= start) and (not end or r["timestamp"] <= end)]


@mcp.tool()
def get_consumption_summary(customer_id: str, period: str = "month") -> dict:
    """Pre-aggregated totals/cost for 'month' or 'year' so the agent never sums raw readings."""
    s = _customer(customer_id)["summary"]
    if period not in s:
        raise ValueError(f"unknown period: {period} (have: {', '.join(s)})")
    return s[period]


def _selftest() -> None:
    assert get_customer_profile("C-1001")["customer_id"] == "C-1001"
    assert any(m["type"] == "electricity" for m in list_meters("C-1001"))
    mid = list_meters("C-1001")[0]["meter_id"]
    all_rows = get_meter_readings(mid)
    window = get_meter_readings(mid, start="2026-07-28", end="2026-07-30")
    assert 0 < len(window) < len(all_rows)
    assert all("2026-07-28" <= r["timestamp"] <= "2026-07-30" for r in window)
    assert get_consumption_summary("C-1001", "month")["total_kwh"] > 0
    for bad in (lambda: get_customer_profile("nope"),
                lambda: get_meter_readings("nope"),
                lambda: get_consumption_summary("C-1001", "decade")):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
    print("selftest ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        mcp.run(transport="streamable-http")
