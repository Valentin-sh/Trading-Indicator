#!/usr/bin/env python3
"""Journal de trading professionnel avec synchronisation Kraken Futures."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

DB_DEFAULT = "trading_journal.db"
KRAKEN_FUTURES_API_URL = "https://futures.kraken.com"


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


def _decode_futures_secret(secret: str) -> bytes:
    """Décodage robuste des secrets Kraken Futures (base64 parfois sans padding)."""
    secret = secret.strip()
    missing_padding = (-len(secret)) % 4
    if missing_padding:
        secret = secret + ("=" * missing_padding)
    try:
        return base64.b64decode(secret)
    except binascii.Error as exc:
        raise RuntimeError(
            "Impossible de décoder KRAKEN_FUTURES_API_SECRET (format base64 invalide)."
        ) from exc


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
            imported_at TEXT NOT NULL,
            source TEXT,
            fill_time_iso TEXT,
            raw_payload TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_raw_trades_time ON raw_trades(timestamp);
        CREATE INDEX IF NOT EXISTS idx_raw_trades_pair ON raw_trades(pair);
        """
    )

    # Migration douce si la base existait déjà (ancienne version spot).
    existing_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(raw_trades)").fetchall()
    }
    for col_name, col_type in [
        ("source", "TEXT"),
        ("fill_time_iso", "TEXT"),
        ("raw_payload", "TEXT"),
    ]:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE raw_trades ADD COLUMN {col_name} {col_type}")

    conn.commit()


def parse_iso_to_ts(iso_value: Optional[str]) -> Optional[float]:
    if not iso_value:
        return None
    normalized = iso_value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def format_ts_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def futures_authent(endpoint_path: str, nonce: str, params_encoded: str, api_secret: str) -> str:
    """Authent Kraken Futures v3:
    1) sha256(postData + nonce + endpointPath)
    2) hmac_sha512(secret_base64_decoded, step1)
    3) base64(step2)
    """
    payload = f"{params_encoded}{nonce}{endpoint_path}".encode("utf-8")
    hash_digest = hashlib.sha256(payload).digest()
    secret_bytes = _decode_futures_secret(api_secret)
    signature = hmac.new(secret_bytes, hash_digest, hashlib.sha512).digest()
    return base64.b64encode(signature).decode("utf-8")


def futures_private_request(
    endpoint_path: str,
    api_key: str,
    api_secret: str,
    params: Optional[Dict[str, str]] = None,
    method: str = "GET",
) -> Dict:
    params = params or {}
    params_encoded = urllib.parse.urlencode(params)
    nonce = str(int(time.time() * 1000))
    authent = futures_authent(endpoint_path, nonce, params_encoded, api_secret)

    headers = {
        "APIKey": api_key,
        "Authent": authent,
        "Nonce": nonce,
        "Accept": "application/json",
    }

    method = method.upper()
    if method == "GET":
        query = f"?{params_encoded}" if params_encoded else ""
        url = f"{KRAKEN_FUTURES_API_URL}{endpoint_path}{query}"
        req_data = None
    else:
        url = f"{KRAKEN_FUTURES_API_URL}{endpoint_path}"
        req_data = params_encoded.encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP error Kraken Futures: {exc.code} {detail}") from exc

    data = json.loads(raw)
    if data.get("result") == "error":
        raise RuntimeError(f"Kraken Futures API error: {data}")

    return data


def fill_to_trade(fill: Dict) -> Trade:
    # Schéma courant Kraken Futures fills: fill_id, order_id, symbol, side, size, price, fillTime, fillType, fee
    trade_id = str(fill.get("fill_id") or fill.get("fillId") or fill.get("uid") or "")
    if not trade_id:
        fallback = f"{fill.get('order_id','na')}:{fill.get('fillTime','na')}:{fill.get('price','na')}"
        trade_id = fallback

    order_id = str(fill.get("order_id") or fill.get("orderId") or "")
    pair = str(fill.get("symbol") or fill.get("instrument") or "UNKNOWN")
    side = str(fill.get("side") or ("buy" if fill.get("buy") else "sell")).lower()
    ordertype = str(fill.get("fillType") or fill.get("fill_type") or "")

    price = float(fill.get("price") or 0.0)
    volume = float(fill.get("size") or fill.get("qty") or 0.0)
    fee = float(fill.get("fee") or 0.0)
    cost = price * volume

    ts = parse_iso_to_ts(fill.get("fillTime"))
    if ts is None:
        ts = float(fill.get("time") or fill.get("timestamp") or time.time())

    return Trade(
        trade_id=trade_id,
        ordertxid=order_id,
        pair=pair,
        side=side,
        ordertype=ordertype,
        price=price,
        cost=cost,
        fee=fee,
        volume=volume,
        timestamp=ts,
    )


def fetch_futures_fills(api_key: str, api_secret: str, last_fill_time: Optional[str]) -> List[Tuple[Trade, Dict]]:
    params: Dict[str, str] = {}
    if last_fill_time:
        params["lastFillTime"] = last_fill_time

    data = futures_private_request(
        endpoint_path="/derivatives/api/v3/fills",
        api_key=api_key,
        api_secret=api_secret,
        params=params,
        method="GET",
    )

    fills = data.get("fills", []) or []
    parsed: List[Tuple[Trade, Dict]] = []
    for fill in fills:
        parsed.append((fill_to_trade(fill), fill))

    return parsed


def save_trades(conn: sqlite3.Connection, trades: Iterable[Tuple[Trade, Dict]]) -> Tuple[int, int]:
    inserted = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    sql = """
        INSERT OR IGNORE INTO raw_trades
        (trade_id, ordertxid, pair, side, ordertype, price, cost, fee, volume, timestamp, imported_at, source, fill_time_iso, raw_payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    cur = conn.cursor()
    for trade, raw in trades:
        fill_time_iso = raw.get("fillTime")
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
                "kraken_futures",
                fill_time_iso,
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        if cur.rowcount == 1:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    return inserted, skipped


def get_last_fill_time(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        """
        SELECT fill_time_iso
        FROM raw_trades
        WHERE source = 'kraken_futures' AND fill_time_iso IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
        """
    ).fetchone()

    if row and row["fill_time_iso"]:
        return str(row["fill_time_iso"])

    # fallback pour les bases plus anciennes sans fill_time_iso
    row2 = conn.execute("SELECT MAX(timestamp) AS max_ts FROM raw_trades").fetchone()
    if row2 and row2["max_ts"] is not None:
        return format_ts_to_iso(float(row2["max_ts"]))
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
        pair_state = positions.setdefault(tr.pair, {"qty": 0.0, "avg": 0.0, "open_time": tr.timestamp})
        qty = pair_state["qty"]
        avg = pair_state["avg"]

        fee_per_unit = (tr.fee / tr.volume) if tr.volume else 0.0

        if tr.side == "buy":
            if qty < 0:  # close short
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
            else:  # open/add long
                new_qty = qty + tr.volume
                avg = ((qty * avg) + (tr.volume * tr.price)) / new_qty if new_qty > 0 else 0.0
                qty = new_qty
                if qty == tr.volume:
                    pair_state["open_time"] = tr.timestamp

        elif tr.side == "sell":
            if qty > 0:  # close long
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
            else:  # open/add short
                new_qty = qty - tr.volume
                short_qty_before = abs(qty)
                short_qty_after = abs(new_qty)
                avg = ((short_qty_before * avg) + (tr.volume * tr.price)) / short_qty_after if short_qty_after > 0 else 0.0
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
        max_dd = max(max_dd, peak - equity)

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
    api_key = os.getenv("KRAKEN_FUTURES_API_KEY") or os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_FUTURES_API_SECRET") or os.getenv("KRAKEN_API_SECRET")

    if not api_key or not api_secret:
        print(
            "Erreur: définir KRAKEN_FUTURES_API_KEY et KRAKEN_FUTURES_API_SECRET "
            "(ou variables legacy KRAKEN_API_KEY/KRAKEN_API_SECRET)",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = connect_db(args.db)
    init_db(conn)

    last_fill_time = None if args.full else get_last_fill_time(conn)
    fills = fetch_futures_fills(api_key, api_secret, last_fill_time=last_fill_time)
    inserted, skipped = save_trades(conn, fills)

    print(
        json.dumps(
            {
                "source": "kraken_futures",
                "lastFillTime_utilise": last_fill_time,
                "recuperes": len(fills),
                "inseres": inserted,
                "deja_presents": skipped,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


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
    pf = stats["profit_factor"]
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
    parser = argparse.ArgumentParser(description="Journal de trading professionnel connecté à Kraken Futures")
    parser.add_argument("--db", default=DB_DEFAULT, help=f"Chemin SQLite (défaut: {DB_DEFAULT})")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Initialiser la base locale")
    p_init.set_defaults(func=cmd_init)

    p_sync = sub.add_parser("sync-kraken", help="Synchroniser les fills Kraken Futures")
    p_sync.add_argument("--full", action="store_true", help="Recharger l'historique complet (sans filtre lastFillTime)")
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
