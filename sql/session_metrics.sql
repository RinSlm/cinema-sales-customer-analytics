-- DRAFT: not executed against the source database.
-- Grain: one row per scheduled session (date, part of day, cinema, room).
-- Run quality_checks.sql first. Schedule and dimension keys
-- must be unique; do not hide duplicate keys with ANY JOIN or DISTINCT.
-- Assumption to verify: one films_payments row represents one sold ticket.
-- Revenue uses the current film price and client discount, matching the
-- original project. These must represent the price/discount at sale time;
-- otherwise use historical transaction amounts when available.
-- Sessions are attributed to session_date, not the payment date.
-- Unmatched payments are reported by the checks, not silently assigned.

WITH
ticket_values AS
(
    SELECT
        fp.session_date,
        fp.part_of_day,
        fp.cinema_id,
        fp.cinema_room_id,
        f.price_ticket * (1 - c.discount_perc / 100.0) AS ticket_price_final,
        isNull(s.film_id)
            OR isNull(f.price_ticket)
            OR isNull(c.discount_perc)
            OR f.price_ticket < 0
            OR c.discount_perc < 0
            OR c.discount_perc > 100 AS invalid_price
    FROM alpha_cinema.films_payments AS fp
    LEFT JOIN alpha_cinema.schedule AS s
        ON fp.session_date = s.session_date
        AND fp.part_of_day = s.part_of_day
        AND fp.cinema_id = s.cinema_id
        AND fp.cinema_room_id = s.cinema_room_id
    LEFT JOIN alpha_cinema.films AS f ON s.film_id = f.film_id
    LEFT JOIN alpha_cinema.clients AS c ON fp.client_id = c.client_id
),
sales_by_session AS
(
    SELECT
        session_date,
        part_of_day,
        cinema_id,
        cinema_room_id,
        count() AS tickets_sold,
        countIf(invalid_price) AS invalid_price_rows,
        sumIf(ticket_price_final, NOT invalid_price) AS known_revenue
    FROM ticket_values
    GROUP BY session_date, part_of_day, cinema_id, cinema_room_id
),
sessions AS
(
    SELECT
        s.session_date,
        toStartOfMonth(s.session_date) AS session_month,
        s.part_of_day,
        s.cinema_id,
        s.cinema_room_id,
        s.film_id,
        f.film_name,
        r.count_seats,
        multiIf(
            isNull(r.is_vip) OR isNull(r.is_3d), 'Unknown',
            r.is_vip = 1 AND r.is_3d = 1, 'VIP+3D',
            r.is_vip = 0 AND r.is_3d = 1, '3D',
            r.is_vip = 1 AND r.is_3d = 0, 'VIP',
            'Standard'
        ) AS cinema_room_type,
        coalesce(p.tickets_sold, 0) AS session_tickets_sold,
        coalesce(p.invalid_price_rows, 0) AS invalid_price_rows,
        -- No purchases => zero revenue. Invalid pricing => unknown revenue.
        if(
            coalesce(p.invalid_price_rows, 0) > 0,
            NULL,
            coalesce(p.known_revenue, 0)
        ) AS session_revenue
    FROM alpha_cinema.schedule AS s
    LEFT JOIN sales_by_session AS p
        ON s.session_date = p.session_date
        AND s.part_of_day = p.part_of_day
        AND s.cinema_id = p.cinema_id
        AND s.cinema_room_id = p.cinema_room_id
    LEFT JOIN alpha_cinema.films AS f ON s.film_id = f.film_id
    LEFT JOIN alpha_cinema.dict_cinema_rooms AS r
        ON s.cinema_id = r.cinema_id
        AND s.cinema_room_id = r.cinema_room_id
)
SELECT
    *,
    if(
        isNull(count_seats) OR count_seats <= 0,
        NULL,
        100.0 * session_tickets_sold / nullIf(count_seats, 0)
    ) AS occupancy_rate_session,
    session_revenue / nullIf(session_tickets_sold, 0) AS avg_ticket_price
FROM sessions
ORDER BY session_date, part_of_day, cinema_id, cinema_room_id
SETTINGS join_use_nulls = 1;

-- BI measures after verifying all relevant seat counts and prices are valid:
-- Seat-weighted occupancy, %: 100 * SUM(session_tickets_sold) / SUM(count_seats)
-- Mean session occupancy, %: AVG(occupancy_rate_session)
-- Ticket revenue per available seat, RUB: SUM(session_revenue) / SUM(count_seats)
-- Ticket revenue per session, RUB: SUM(session_revenue) / COUNT(session rows)
-- Do not call revenue per available seat profitability: costs are not modeled.
-- Do not sum across a subset of missing prices/capacities without disclosing it.
