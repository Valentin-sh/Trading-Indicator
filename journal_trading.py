#!/usr/bin/env python3
"""Journal de trading professionnel avec synchronisation Kraken Pro.

Fonctionnalités:
- Synchronisation automatique des trades Kraken Pro via API privée.
- Stockage local SQLite pour historiser les données.
- Reconstruction des performances (win rate, gain moyen, perte moyenne, expectancy, profit factor, drawdown).
- Export CSV.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import urllib.error
import urllib.request

DB_DEFAULT = "trading_journal.db"
API_URL = "https://api.kraken.com"


@dataclass
class Trade:
    trade_id: str
    ordertxid: str
    pair: str
    side: str
    ordertype: str
    price: float
    cost: float
    fee: float
    volume: float
    timestamp: float


@dataclass
class ClosedLot:
    pair: str
    side_closed: str
    quantity: float
    entry_price: float
    exit_price: float
    pnl: float
    open_time: float
    close_time: float


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_trades (
            trade_id TEXT PRIMARY KEY,
            ordertxid TEXT,
            pair TEXT NOT NULL,
            side TEXT NOT NULL,
            ordertype TEXT,
            price REAL NOT NULL,
            cost REAL NOT NULL,
            fee REAL NOT NULL,
            volume REAL NOT NULL,
            timestamp REAL NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_raw_trades_time ON raw_trades(timestamp);
        CREATE INDEX IF NOT EXISTS idx_raw_trades_pair ON raw_trades(pair);
        """
    )
    conn.commit()


def kraken_signature(url_path: str, data: Dict[str, str], api_secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = url_path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(api_secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def kraken_private_request(endpoint: str, api_key: str, api_secret: str, payload: Dict[str, str]) -> Dict:
    url_path = f"/0/private/{endpoint}"
    url = f"{API_URL}{url_path}"

    payload = dict(payload)
    payload["nonce"] = str(int(time.time() * 1000))

    headers = {
        "API-Key": api_key,
        "API-Sign": kraken_signature(url_path, payload, api_secret),
    }

    data_encoded = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data_encoded, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP error Kraken: {exc.code} {detail}") from exc

    data = json.loads(raw)
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    return data["result"]


def fetch_all_trades(api_key: str, api_secret: str, start_ts: float | None = None) -> List[Trade]:
    all_trades: List[Trade] = []
    ofs = 0

    while True:
        payload: Dict[str, str] = {"ofs": str(ofs), "type": "all", "trades": "true"}
        if start_ts is not None:
            payload["start"] = str(int(start_ts))

        result = kraken_private_request("TradesHistory", api_key, api_secret, payload)
        trades_block = result.get("trades", {})
        count = int(result.get("count", 0))

        for trade_id, t in trades_block.items():
            all_trades.append(
                Trade(
                    trade_id=trade_id,
                    ordertxid=t.get("ordertxid", ""),
                    pair=t["pair"],
                    side=t["type"],
                    ordertype=t.get("ordertype", ""),
                    price=float(t["price"]),
                    cost=float(t["cost"]),
                    fee=float(t["fee"]),
                    volume=float(t["vol"]),
                    timestamp=float(t["time"]),
                )
            )

        ofs += len(trades_block)
        if ofs >= count or not trades_block:
            break

        time.sleep(1)

    return all_trades


def save_trades(conn: sqlite3.Connection, trades: Iterable[Trade]) -> Tuple[int, int]:
    inserted = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    sql = """
        INSERT OR IGNORE INTO raw_trades
        (trade_id, ordertxid, pair, side, ordertype, price, cost, fee, volume, timestamp, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    cur = conn.cursor()
    for trade in trades:
        cur.execute(
            sql,
            (
                trade.trade_id,
                trade.ordertxid,
                trade.pair,
                trade.side,
                trade.ordertype,
                trade.price,
                trade.cost,
                trade.fee,
                trade.volume,
                trade.timestamp,
                now,
            ),
        )
        if cur.rowcount == 1:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    return inserted, skipped


def get_last_timestamp(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT MAX(timestamp) AS max_ts FROM raw_trades").fetchone()
    if row and row["max_ts"] is not None:
        return float(row["max_ts"])
    return None


def load_trades(conn: sqlite3.Connection) -> List[Trade]:
    rows = conn.execute(
        """
        SELECT trade_id, ordertxid, pair, side, ordertype, price, cost, fee, volume, timestamp
        FROM raw_trades
        ORDER BY timestamp ASC, trade_id ASC
        """
    ).fetchall()
    return [
        Trade(
            trade_id=row["trade_id"],
            ordertxid=row["ordertxid"],
            pair=row["pair"],
            side=row["side"],
            ordertype=row["ordertype"],
            price=float(row["price"]),
            cost=float(row["cost"]),
            fee=float(row["fee"]),
            volume=float(row["volume"]),
            timestamp=float(row["timestamp"]),
        )
        for row in rows
    ]


def reconstruct_closed_lots(trades: List[Trade]) -> List[ClosedLot]:
    positions: Dict[str, Dict[str, float]] = {}
    closed: List[ClosedLot] = []

    for tr in trades:
        pair_state = positions.setdefault(
            tr.pair,
            {"qty": 0.0, "avg": 0.0, "open_time": tr.timestamp},
        )

        qty = pair_state["qty"]
        avg = pair_state["avg"]

        fee_per_unit = (tr.fee / tr.volume) if tr.volume else 0.0

        if tr.side == "buy":
            if qty < 0:
                close_qty = min(tr.volume, abs(qty))
                pnl = (avg - tr.price) * close_qty - (fee_per_unit * close_qty)
                closed.append(
                    ClosedLot(
                        pair=tr.pair,
                        side_closed="short",
                        quantity=close_qty,
                        entry_price=avg,
                        exit_price=tr.price,
                        pnl=pnl,
                        open_time=pair_state["open_time"],
                        close_time=tr.timestamp,
                    )
                )
                qty += close_qty
                remaining = tr.volume - close_qty
                if remaining > 0:
                    qty = remaining
                    avg = tr.price
                    pair_state["open_time"] = tr.timestamp
                elif qty == 0:
                    avg = 0.0
            else:
                new_qty = qty + tr.volume
                if new_qty > 0:
                    avg = ((qty * avg) + (tr.volume * tr.price)) / new_qty
                qty = new_qty
                if qty == tr.volume:
                    pair_state["open_time"] = tr.timestamp

        elif tr.side == "sell":
            if qty > 0:
                close_qty = min(tr.volume, qty)
                pnl = (tr.price - avg) * close_qty - (fee_per_unit * close_qty)
                closed.append(
                    ClosedLot(
                        pair=tr.pair,
                        side_closed="long",
                        quantity=close_qty,
                        entry_price=avg,
                        exit_price=tr.price,
                        pnl=pnl,
                        open_time=pair_state["open_time"],
                        close_time=tr.timestamp,
                    )
                )
                qty -= close_qty
                remaining = tr.volume - close_qty
                if remaining > 0:
                    qty = -remaining
                    avg = tr.price
                    pair_state["open_time"] = tr.timestamp
                elif qty == 0:
                    avg = 0.0
            else:
                new_qty = qty - tr.volume
                short_qty_before = abs(qty)
                short_qty_after = abs(new_qty)
                if short_qty_after > 0:
                    avg = ((short_qty_before * avg) + (tr.volume * tr.price)) / short_qty_after
                qty = new_qty
                if qty == -tr.volume:
                    pair_state["open_time"] = tr.timestamp

        pair_state["qty"] = qty
        pair_state["avg"] = avg

    return closed


def compute_stats(closed_lots: List[ClosedLot]) -> Dict[str, float]:
    if not closed_lots:
        return {
            "nb_trades_fermes": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "pnl_total": 0.0,
            "max_drawdown": 0.0,
        }

    wins = [x.pnl for x in closed_lots if x.pnl > 0]
    losses = [x.pnl for x in closed_lots if x.pnl <= 0]

    pnl_total = sum(x.pnl for x in closed_lots)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for c in closed_lots:
        equity += c.pnl
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    return {
        "nb_trades_fermes": len(closed_lots),
        "win_rate": (len(wins) / len(closed_lots)) * 100,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "expectancy": pnl_total / len(closed_lots),
        "pnl_total": pnl_total,
        "max_drawdown": max_dd,
    }


def cmd_init(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    init_db(conn)
    print(f"Base initialisée: {args.db}")


def cmd_sync(args: argparse.Namespace) -> None:
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_API_SECRET")

    if not api_key or not api_secret:
        print("Erreur: définir KRAKEN_API_KEY et KRAKEN_API_SECRET", file=sys.stderr)
        sys.exit(1)

    conn = connect_db(args.db)
    init_db(conn)

    start_ts = None if args.full else get_last_timestamp(conn)
    trades = fetch_all_trades(api_key, api_secret, start_ts=start_ts)
    inserted, skipped = save_trades(conn, trades)

    print(json.dumps({"recuperes": len(trades), "inseres": inserted, "deja_presents": skipped}, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    trades = load_trades(conn)
    closed = reconstruct_closed_lots(trades)
    stats = compute_stats(closed)

    print("=== Rapport Journal Trading ===")
    print(f"Trades bruts: {len(trades)}")
    print(f"Trades fermés reconstruits: {int(stats['nb_trades_fermes'])}")
    print(f"Win rate: {stats['win_rate']:.2f}%")
    print(f"Gain moyen (wins): {stats['avg_win']:.4f}")
    print(f"Perte moyenne (losses): {stats['avg_loss']:.4f}")
    pf = stats['profit_factor']
    pf_str = "∞" if pf == float("inf") else f"{pf:.4f}"
    print(f"Profit factor: {pf_str}")
    print(f"Expectancy par trade: {stats['expectancy']:.4f}")
    print(f"PnL total: {stats['pnl_total']:.4f}")
    print(f"Max drawdown: {stats['max_drawdown']:.4f}")


def cmd_export(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    trades = load_trades(conn)
    closed = reconstruct_closed_lots(trades)

    output = Path(args.output)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "pair",
                "side_closed",
                "quantity",
                "entry_price",
                "exit_price",
                "pnl",
                "open_time_utc",
                "close_time_utc",
            ]
        )
        for c in closed:
            writer.writerow(
                [
                    c.pair,
                    c.side_closed,
                    c.quantity,
                    c.entry_price,
                    c.exit_price,
                    c.pnl,
                    datetime.fromtimestamp(c.open_time, tz=timezone.utc).isoformat(),
                    datetime.fromtimestamp(c.close_time, tz=timezone.utc).isoformat(),
                ]
            )

    print(f"Export terminé: {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Journal de trading professionnel connecté à Kraken Pro")
    parser.add_argument("--db", default=DB_DEFAULT, help=f"Chemin SQLite (défaut: {DB_DEFAULT})")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Initialiser la base locale")
    p_init.set_defaults(func=cmd_init)

    p_sync = sub.add_parser("sync-kraken", help="Synchroniser les trades Kraken Pro")
    p_sync.add_argument("--full", action="store_true", help="Recharger l'historique complet")
    p_sync.set_defaults(func=cmd_sync)

    p_report = sub.add_parser("report", help="Afficher les métriques pro")
    p_report.set_defaults(func=cmd_report)

    p_export = sub.add_parser("export-csv", help="Exporter les trades fermés reconstruits")
    p_export.add_argument("--output", default="closed_trades.csv", help="Fichier CSV de sortie")
    p_export.set_defaults(func=cmd_export)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
