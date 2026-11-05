"""Build Cinema facts and metrics from six raw CSV exports, without credentials.

One films_payments row is treated as one sold ticket; attendance is not known.
Identical ticket rows are preserved. Bar rows are preserved by default; an
explicit exact policy reproduces the notebook's four-column drop_duplicates().
Raw clients are read only for client_id and discount_perc (no personal fields).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
KEY = ["session_date", "part_of_day", "cinema_id", "cinema_room_id"]
SCHEMAS = {
    "schedule": KEY + ["film_id"],
    "films_payments": KEY + ["client_id", "type_pay", "pay_at"],
    "films": ["film_id", "film_name", "price_ticket"],
    "clients": ["client_id", "discount_perc"],
    "dict_cinema_rooms": ["cinema_id", "cinema_room_id", "count_seats", "is_vip", "is_3d"],
    "bar_payments": ["client_id", "pay_at", "name", "price"],
}


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def read_table(folder: Path, name: str) -> pd.DataFrame:
    path = folder / f"{name}.csv"
    if not path.exists() and name == "films_payments":
        path = folder / "films_payment.csv"
    require(path.exists(), f"Missing raw file: {name}.csv")
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            # Delimiter sniffing handles ordinary comma, semicolon and tab CSV.
            # Only required columns are retained; personal client fields are
            # never included in intermediate facts or published outputs.
            df = pd.read_csv(
                path, sep=None, engine="python", dtype="string", encoding=encoding,
                na_values=["[NULL]", "NULL", "\\N"],
                usecols=lambda col: col.strip().lstrip("\ufeff") in SCHEMAS[name],
            )
            break
        except UnicodeDecodeError:
            if encoding == "cp1251":
                raise
    df.columns = df.columns.str.strip().str.lstrip("\ufeff")
    missing = set(SCHEMAS[name]) - set(df.columns)
    require(not missing, f"{name}: missing columns {sorted(missing)}")
    df = df[SCHEMAS[name]].copy()
    for col in df:
        df[col] = df[col].str.strip()
    require(not df.isna().any().any(), f"{name}: null required values")
    require(not df.eq("").any().any(), f"{name}: blank required values")
    return df


def number(values: pd.Series, label: str, integer: bool = False) -> pd.Series:
    raw = values.astype("string").str.replace("\u00a0", "", regex=False).str.replace(" ", "", regex=False)
    if integer:
        # Accept exported display-style integer grouping such as 1,234,567.
        raw = raw.map(lambda v: v.replace(",", "")
                      if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", v) else v)
    raw = raw.str.replace(",", ".", regex=False)
    result = pd.to_numeric(raw, errors="coerce")
    require(bool(result.notna().all()), f"{label}: invalid numeric values")
    if integer:
        require(bool((result % 1 == 0).all()), f"{label}: non-integer values")
        result = result.astype("int64")
    return result


def session_dates(values: pd.Series) -> pd.Series:
    # A session_date is a local calendar date. Preserve its YYYY-MM-DD prefix,
    # including exports with timezone decorations, rather than shifting it.
    prefix = values.astype("string").str.extract(r"^(\d{4}-\d{2}-\d{2})", expand=False)
    require(bool(prefix.notna().all()), "session_date must start with YYYY-MM-DD")
    result = pd.to_datetime(prefix, format="%Y-%m-%d", errors="coerce")
    require(bool(result.notna().all()), "Invalid session_date")
    return result


def local_times(values: pd.Series, naive_timezone: str) -> pd.Series:
    def convert(value: str) -> pd.Timestamp:
        t = pd.Timestamp(value)
        if t.tzinfo is None:
            t = t.tz_localize(naive_timezone, ambiguous="raise", nonexistent="raise")
        return t.tz_convert("Europe/Moscow").tz_localize(None)
    try:
        result = pd.to_datetime(values.map(convert))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid pay_at value or timezone") from exc
    require(bool(result.notna().all()), "Invalid pay_at value: missing timestamp")
    return result


def unique(df: pd.DataFrame, key: list[str], label: str) -> None:
    require(not df.duplicated(key).any(), f"{label}: duplicate keys {key}")


def attach(left: pd.DataFrame, right: pd.DataFrame, key: list[str], label: str) -> pd.DataFrame:
    merged = left.merge(right, on=key, how="left", validate="many_to_one", indicator=True)
    require(bool(merged._merge.eq("both").all()), f"Unmatched references: {label}")
    require(len(merged) == len(left), f"JOIN changed row count: {label}")
    return merged.drop(columns="_merge")


def key_strings(df: pd.DataFrame) -> pd.Series:
    return (df.session_date.dt.strftime("%Y-%m-%d") + " " + df.part_of_day + " "
            + df.cinema_id + " " + df.cinema_room_id.astype(str))


def segment(values: pd.Series) -> pd.Series:
    return values.map(lambda x: "1 сеанс" if x == 1 else "2–3 сеанса" if x <= 3 else "4+ сеанса")


def build(raw_dir: Path, output_dir: Path, naive_timezone: str = "Europe/Moscow",
          bar_duplicate_policy: str = "preserve") -> dict:
    require(bar_duplicate_policy in {"preserve", "exact"}, "Unknown bar duplicate policy")
    raw = {name: read_table(raw_dir, name) for name in SCHEMAS}
    sch, pay, films, clients, rooms, bar = (raw[name] for name in SCHEMAS)
    for name, df in raw.items():
        for col in ("film_id", "client_id", "cinema_room_id", "count_seats"):
            if col in df:
                df[col] = number(df[col], f"{name}.{col}", integer=True)
        for col in ("price_ticket", "discount_perc", "price"):
            if col in df:
                df[col] = number(df[col], f"{name}.{col}")
        if "session_date" in df:
            df["session_date"] = session_dates(df.session_date)
        if "pay_at" in df:
            df["pay_at"] = local_times(df.pay_at, naive_timezone)

    unique(sch, KEY, "schedule")
    unique(films, ["film_id"], "films")
    unique(clients, ["client_id"], "clients")
    unique(rooms, ["cinema_id", "cinema_room_id"], "dict_cinema_rooms")
    require(bool((rooms.count_seats > 0).all()), "Room capacity must be positive")
    require(bool((films.price_ticket >= 0).all()), "Negative film price")
    require(bool(clients.discount_perc.between(0, 100).all()), "Discount outside [0,100]")
    require(bool((bar.price >= 0).all()), "Negative bar item amount")
    raw_bar = bar.copy()
    exact_duplicate_mask = raw_bar.duplicated(keep="first")
    bar_duplicate_candidates = raw_bar.loc[raw_bar.duplicated(keep=False)].copy()
    # This is a declared cleaning rule, not proof that identical product lines
    # are technical duplicates. It matches Project_v2.ipynb when selected.
    if bar_duplicate_policy == "exact":
        bar = raw_bar.loc[~exact_duplicate_mask].copy()
    bool_map = {"true": True, "false": False, "1": True, "0": False,
                "t": True, "f": False, "yes": True, "no": False,
                "да": True, "нет": False}
    for col in ("is_vip", "is_3d"):
        rooms[col] = rooms[col].str.casefold().map(bool_map)
        require(bool(rooms[col].notna().all()), f"Unknown boolean {col}")

    # All dimensions validated before a single ticket is joined.
    schedule = attach(sch, films, ["film_id"], "schedule -> film")
    schedule = attach(schedule, rooms, ["cinema_id", "cinema_room_id"], "schedule -> room")
    schedule["session_key"] = key_strings(schedule)
    unique(schedule, ["session_key"], "formatted session key")
    schedule["cinema_room_type"] = "Standard"
    schedule.loc[schedule.is_3d, "cinema_room_type"] = "3D"
    schedule.loc[schedule.is_vip, "cinema_room_type"] = "VIP"
    schedule.loc[schedule.is_vip & schedule.is_3d, "cinema_room_type"] = "VIP+3D"

    tickets = attach(pay, schedule[KEY + ["session_key", "film_id", "film_name", "price_ticket",
                                        "count_seats", "cinema_room_type"]], KEY, "ticket -> session")
    tickets = attach(tickets, clients, ["client_id"], "ticket -> client")
    tickets["discount_amount"] = tickets.price_ticket * tickets.discount_perc / 100
    tickets["ticket_price_final"] = tickets.price_ticket - tickets.discount_amount
    tickets["sales_channel"] = tickets.type_pay.str.casefold().map(
        {"сайт": "Online", "касса": "Offline"}).fillna("Unknown")
    tickets["payment_month_start"] = tickets.pay_at.dt.to_period("M").dt.to_timestamp()
    tickets["session_month_start"] = tickets.session_date.dt.to_period("M").dt.to_timestamp()

    agg = tickets.groupby("session_key", as_index=False).agg(
        session_tickets_sold=("client_id", "size"),
        session_revenue=("ticket_price_final", "sum"),
        avg_ticket_price=("ticket_price_final", "mean"),
        total_discount=("discount_amount", "sum"),
        online_tickets=("sales_channel", lambda x: int(x.eq("Online").sum())),
        unknown_channel_tickets=("sales_channel", lambda x: int(x.eq("Unknown").sum())),
    )
    sessions = schedule.merge(agg, on="session_key", how="left", validate="one_to_one")
    for col in ("session_tickets_sold", "online_tickets", "unknown_channel_tickets"):
        sessions[col] = sessions[col].fillna(0).astype("int64")
    for col in ("session_revenue", "total_discount"):
        sessions[col] = sessions[col].fillna(0)
    # avg_ticket_price remains null for a session without tickets (undefined).
    require(bool((sessions.session_tickets_sold <= sessions.count_seats).all()),
            "Sold tickets exceed capacity; validate ticket grain or refunds")
    sessions["occupancy_rate_session"] = 100 * sessions.session_tickets_sold / sessions.count_seats
    sessions["online_share_session"] = (
        100 * sessions.online_tickets / sessions.session_tickets_sold.replace(0, float("nan")))
    sessions["month"] = sessions.session_date.dt.month
    sessions["month_start"] = sessions.session_date.dt.to_period("M").dt.to_timestamp()
    sessions["session_month"] = sessions.session_date.dt.strftime("%Y-%m")
    sessions["revenue_per_available_seat"] = sessions.session_revenue / sessions.count_seats
    sessions["observation_scope"] = "Все сеансы расписания"
    sessions = sessions.drop(columns=["is_vip", "is_3d"])

    customer = tickets.groupby("client_id", as_index=False).agg(
        tickets_bought=("session_key", "size"), sessions_visited=("session_key", "nunique"),
        total_ticket_revenue=("ticket_price_final", "sum"), avg_ticket_price=("ticket_price_final", "mean"),
        total_discount=("discount_amount", "sum"), first_purchase=("pay_at", "min"), last_purchase=("pay_at", "max"),
    )
    customer["client_type"] = segment(customer.sessions_visited)
    # Aggregate the rows retained by the explicit bar policy. A product line
    # is not necessarily an order.
    bar_metrics = bar.groupby("client_id", as_index=False).agg(
        bar_item_rows_count=("price", "size"), bar_revenue=("price", "sum"),
        bar_avg_item_price=("price", "mean"), first_bar_purchase=("pay_at", "min"),
        last_bar_purchase=("pay_at", "max"),
    )
    customer = customer.merge(bar_metrics, on="client_id", how="left", validate="one_to_one")
    customer["bar_item_rows_count"] = customer.bar_item_rows_count.fillna(0).astype("int64")
    customer["bar_revenue"] = customer.bar_revenue.fillna(0)
    customer["has_bar_purchase"] = customer.bar_item_rows_count.gt(0).astype("int64")
    # No bar_avg_item_price=0 fill: no bar purchase has an undefined item average.
    customer["total_revenue_with_bar"] = customer.total_ticket_revenue + customer.bar_revenue
    customer["ticket_revenue_per_observed_session"] = customer.total_ticket_revenue / customer.sessions_visited

    total_ticket_revenue = float(tickets.ticket_price_final.sum())
    require(abs(sessions.session_revenue.sum() - total_ticket_revenue) < 0.01,
            "Session revenue reconciliation failed")
    require(abs(customer.total_ticket_revenue.sum() - total_ticket_revenue) < 0.01,
            "Client ticket revenue reconciliation failed")
    require(int(sessions.session_tickets_sold.sum()) == len(pay), "Session ticket count reconciliation failed")
    require(int(customer.tickets_bought.sum()) == len(pay), "Client ticket count reconciliation failed")
    require(len(sessions) == len(sch), "Schedule row count changed")
    require(abs(bar_metrics.bar_revenue.sum() - bar.price.sum()) < 0.01,
            "Retained bar sum reconciliation failed")
    require(abs(float(raw_bar.price.sum()) - float(bar.price.sum()) -
                float(raw_bar.loc[exact_duplicate_mask, "price"].sum()
                      if bar_duplicate_policy == "exact" else 0)) < 0.01,
            "Bar cleaning before/after reconciliation failed")

    monthly_sessions = sessions.groupby("month_start", as_index=False).agg(
        scheduled_sessions=("session_key", "size"), tickets=("session_tickets_sold", "sum"),
        ticket_revenue=("session_revenue", "sum"), available_seats=("count_seats", "sum"),
    )
    monthly_sessions["occupancy_pct"] = 100 * monthly_sessions.tickets / monthly_sessions.available_seats
    monthly_payments = tickets.groupby("payment_month_start", as_index=False).agg(
        ticket_rows=("session_key", "size"), ticket_revenue=("ticket_price_final", "sum"),
    )
    cinema = sessions.groupby("cinema_id", as_index=False).agg(
        scheduled_sessions=("session_key", "size"), tickets=("session_tickets_sold", "sum"),
        ticket_revenue=("session_revenue", "sum"), available_seats=("count_seats", "sum"),
        online_tickets=("online_tickets", "sum"),
    )
    cinema["occupancy_pct"] = 100 * cinema.tickets / cinema.available_seats
    cinema["revenue_per_available_seat"] = cinema.ticket_revenue / cinema.available_seats
    cinema["revenue_per_scheduled_session"] = cinema.ticket_revenue / cinema.scheduled_sessions
    cinema["online_share_pct"] = 100 * cinema.online_tickets / cinema.tickets.replace(0, float("nan"))

    matched_bar = bar.client_id.isin(customer.client_id)
    report = {
        "verification_scope": "Six supplied raw CSV exports, read locally; no live-database query",
        "source_rows": {name: len(df) for name, df in raw.items()},
        "naive_pay_at_timezone_assumption": naive_timezone,
        "output_timezone": "Europe/Moscow (timestamps written without timezone suffix)",
        "checks_passed": ["Required columns and non-null values", "Unique schedule and dimension keys",
                          "All ticket references matched many-to-one", "Positive seat capacities and valid price/discount ranges",
                          "Sold tickets do not exceed capacity", "Every scheduled session retained",
                          "Ticket count/revenue reconcile across ticket, session and client tables",
                          ("All raw bar rows and raw bar revenue preserved in bar item/aggregate outputs"
                           if bar_duplicate_policy == "preserve" else
                           "Exact four-column bar cleaning matches the selected notebook policy"),
                          "Bar retained revenue plus removed revenue reconciles to raw revenue"],
        "schedule_sessions": len(sessions),
        "sessions_without_sales": int(sessions.session_tickets_sold.eq(0).sum()),
        "ticket_rows": len(tickets), "ticket_revenue": total_ticket_revenue,
        "identical_ticket_rows_preserved": int(pay.duplicated().sum()),
        "unknown_channel_ticket_rows": int(tickets.sales_channel.eq("Unknown").sum()),
        "ticket_customers": len(customer),
        "bar": {
            "duplicate_policy": bar_duplicate_policy,
            "duplicate_key": SCHEMAS["bar_payments"],
            "source_item_rows": len(raw_bar),
            "exact_duplicate_rows_in_source": int(exact_duplicate_mask.sum()),
            "retained_item_rows": len(bar),
            "removed_item_rows": len(raw_bar) - len(bar),
            "removed_bar_revenue": float(raw_bar.price.sum() - bar.price.sum()),
            "raw_item_rows_preserved": len(bar), "identical_rows_preserved": int(bar.duplicated().sum()),
            "all_raw_bar_revenue": float(raw_bar.price.sum()),
            "bar_revenue_after_cleaning": float(bar.price.sum()),
            "bar_revenue_of_ticket_customers": float(bar.loc[matched_bar, "price"].sum()),
            "bar_revenue_outside_ticket_customer_base": float(bar.loc[~matched_bar, "price"].sum()),
            "bar_rows_outside_ticket_customer_base": int((~matched_bar).sum()),
            "bar_customers_not_in_client_master": int(bar.loc[~bar.client_id.isin(clients.client_id), "client_id"].nunique()),
            "first_bar_payment": str(bar.pay_at.min()), "last_bar_payment": str(bar.pay_at.max()),
        },
        "dates": {"first_scheduled_session": str(sessions.session_date.min().date()),
                  "last_scheduled_session": str(sessions.session_date.max().date()),
                  "first_ticket_payment": str(tickets.pay_at.min()), "last_ticket_payment": str(tickets.pay_at.max())},
        "remaining_assumptions": [
            "Each films_payments row is one sold ticket; identical rows are not deleted",
            "A purchased session is not verified attendance",
            "Film price and client discount reflect the transaction-time values; history is unavailable",
            ("Identical bar rows may be repeated units or duplicates; preserved pending source clarification"
             if bar_duplicate_policy == "preserve" else
             "Exact bar duplicates removed by the declared Project_v2 notebook rule; technical duplication is not independently established"),
            "Bar rows are item records; order counts and average receipt are not claimed",
            "Bar has no cinema/session key: no allocation to a cinema, film or individual visit is performed",
            "Combined customer revenue covers supplied bar exports and ticket customers only; bar and ticket windows may differ",
            "Observed client frequency does not establish new-customer status or retention probability",
        ],
        "datalens_compatibility_aliases": {
            "client_metrics_datalens.csv": {
                "bar_orders_count": "Legacy alias of bar_item_rows_count; counts item rows, NOT orders",
                "bar_avg_check": "Legacy alias of bar_avg_item_price; mean item price, NOT average receipt",
            },
            "session_metrics_datalens.csv": {
                "session_date": "YYYY-MM-DD calendar-date string, matching the prior CSV source schema",
            },
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    frames = {"ticket_fact.csv": tickets, "session_metrics.csv": sessions,
              "client_metrics.csv": customer, "bar_items.csv": bar,
              "bar_duplicate_candidates.csv": bar_duplicate_candidates,
              "bar_client_metrics.csv": bar_metrics, "monthly_sessions.csv": monthly_sessions,
              "monthly_payments.csv": monthly_payments, "cinema_comparison.csv": cinema}
    for name, frame in frames.items():
        frame.to_csv(output_dir / name, index=False, date_format="%Y-%m-%d %H:%M:%S")
    # Separate compatibility exports preserve old DataLens field names. Their
    # aliases are technical schema compatibility only; chart labels must use
    # the corrected item-row / item-price interpretation.
    session_compat = sessions.copy()
    session_compat["session_date"] = session_compat.session_date.dt.strftime("%Y-%m-%d")
    session_compat["month_start"] = session_compat.month_start.dt.strftime("%Y-%m-%d")
    old_session_columns = [
        "session_key", "session_date", "month", "cinema_id", "cinema_room_id", "cinema_room_type",
        "film_id", "film_name", "count_seats", "session_tickets_sold", "session_revenue",
        "occupancy_rate_session", "avg_ticket_price", "total_discount", "online_tickets",
        "online_share_session",
    ]
    session_compat = session_compat[old_session_columns + [
        name for name in session_compat.columns if name not in old_session_columns]]
    session_compat.to_csv(output_dir / "session_metrics_datalens.csv", index=False)
    client_compat = customer.assign(
        bar_orders_count=customer.bar_item_rows_count,
        bar_avg_check=customer.bar_avg_item_price,
    )
    old_client_columns = [
        "client_id", "tickets_bought", "sessions_visited", "total_ticket_revenue", "avg_ticket_price",
        "total_discount", "first_purchase", "last_purchase", "client_type", "bar_orders_count",
        "bar_revenue", "bar_avg_check", "has_bar_purchase", "total_revenue_with_bar",
    ]
    client_compat = client_compat[old_client_columns + [
        name for name in client_compat.columns if name not in old_client_columns]]
    client_compat.to_csv(output_dir / "client_metrics_datalens.csv", index=False,
                         date_format="%Y-%m-%d %H:%M:%S")
    (output_dir / "raw_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True,
                        help="Folder containing the six raw table CSV exports")
    parser.add_argument("--output", type=Path, default=BASE.parent / "data" / "processed",
                        help="Separate output folder; aggregate-only outputs remain untouched")
    parser.add_argument("--naive-timezone", default="Europe/Moscow",
                        help="Timezone of timestamps exported without an offset; use UTC if exported as UTC")
    parser.add_argument("--bar-duplicate-policy", choices=("preserve", "exact"), default="preserve",
                        help="preserve keeps all bar rows; exact reproduces the notebook's four-column drop_duplicates()")
    args = parser.parse_args()
    result = build(args.raw_dir, args.output, args.naive_timezone, args.bar_duplicate_policy)
    print(f"Raw build validated: {result['schedule_sessions']} scheduled sessions, "
          f"{result['sessions_without_sales']} without sales, {result['ticket_rows']} ticket rows. "
          f"Outputs: {args.output}")
