-- DRAFT: not executed. Read-only checks; run statements separately.
-- Empty anomaly tables / zero missing counts are expected, not assumed results.

-- 1. Unique schedule key: duplicates would multiply ticket sales in a JOIN.
SELECT session_date, part_of_day, cinema_id, cinema_room_id, count() AS rows
FROM alpha_cinema.schedule
GROUP BY session_date, part_of_day, cinema_id, cinema_room_id
HAVING rows > 1;

-- 2. Unique dimension keys: investigate duplicates before aggregation.
SELECT film_id, count() AS rows
FROM alpha_cinema.films GROUP BY film_id HAVING rows > 1;

SELECT client_id, count() AS rows
FROM alpha_cinema.clients GROUP BY client_id HAVING rows > 1;

SELECT cinema_id, cinema_room_id, count() AS rows
FROM alpha_cinema.dict_cinema_rooms
GROUP BY cinema_id, cinema_room_id HAVING rows > 1;

-- 3. Missing foreign keys. Only interpret row counts after checks 1-2 pass.
SELECT count() AS unmatched_payment_rows
FROM alpha_cinema.films_payments AS fp
LEFT JOIN (SELECT *, 1 AS matched FROM alpha_cinema.schedule) AS s
    ON fp.session_date = s.session_date AND fp.part_of_day = s.part_of_day
    AND fp.cinema_id = s.cinema_id AND fp.cinema_room_id = s.cinema_room_id
WHERE isNull(s.matched)
SETTINGS join_use_nulls = 1;

SELECT count() AS payment_rows_without_client
FROM alpha_cinema.films_payments AS fp
LEFT JOIN (SELECT client_id, 1 AS matched FROM alpha_cinema.clients) AS c
    ON fp.client_id = c.client_id
WHERE isNull(c.matched)
SETTINGS join_use_nulls = 1;

SELECT count() AS sessions_without_film
FROM alpha_cinema.schedule AS s
LEFT JOIN (SELECT film_id, 1 AS matched FROM alpha_cinema.films) AS f
    ON s.film_id = f.film_id
WHERE isNull(f.matched)
SETTINGS join_use_nulls = 1;

SELECT count() AS sessions_without_room
FROM alpha_cinema.schedule AS s
LEFT JOIN
    (SELECT cinema_id, cinema_room_id, 1 AS matched
     FROM alpha_cinema.dict_cinema_rooms) AS r
    ON s.cinema_id = r.cinema_id AND s.cinema_room_id = r.cinema_room_id
WHERE isNull(r.matched)
SETTINGS join_use_nulls = 1;

-- 4. JOIN reconciliation: joined_rows must equal source_payment_rows.
-- Revenue is reconstructed, not an independently stored cash reconciliation.
SELECT
    (SELECT count() FROM alpha_cinema.films_payments) AS source_payment_rows,
    count() AS joined_rows,
    countIf(isNull(f.price_ticket) OR isNull(c.discount_perc)) AS unknown_prices,
    sum(f.price_ticket * (1 - c.discount_perc / 100.0)) AS reconstructed_revenue
FROM alpha_cinema.films_payments AS fp
LEFT JOIN alpha_cinema.schedule AS s
    ON fp.session_date = s.session_date AND fp.part_of_day = s.part_of_day
    AND fp.cinema_id = s.cinema_id AND fp.cinema_room_id = s.cinema_room_id
LEFT JOIN alpha_cinema.films AS f ON s.film_id = f.film_id
LEFT JOIN alpha_cinema.clients AS c ON fp.client_id = c.client_id
LEFT JOIN alpha_cinema.dict_cinema_rooms AS r
    ON fp.cinema_id = r.cinema_id AND fp.cinema_room_id = r.cinema_room_id
SETTINGS join_use_nulls = 1;

-- 5. Pricing and capacity bounds. Nulls are not zero-price tickets.
SELECT film_id, price_ticket FROM alpha_cinema.films
WHERE isNull(price_ticket) OR price_ticket < 0;

SELECT client_id, discount_perc FROM alpha_cinema.clients
WHERE isNull(discount_perc) OR discount_perc < 0 OR discount_perc > 100;

SELECT cinema_id, cinema_room_id, count_seats
FROM alpha_cinema.dict_cinema_rooms
WHERE isNull(count_seats) OR count_seats <= 0;

-- 6. Tickets above capacity. Assumes one payment row = one ticket, no returns.
SELECT p.*, r.count_seats
FROM
(
    SELECT session_date, part_of_day, cinema_id, cinema_room_id,
        count() AS tickets_sold
    FROM alpha_cinema.films_payments
    GROUP BY session_date, part_of_day, cinema_id, cinema_room_id
) AS p
LEFT JOIN alpha_cinema.dict_cinema_rooms AS r
    ON p.cinema_id = r.cinema_id AND p.cinema_room_id = r.cinema_room_id
WHERE p.tickets_sold > r.count_seats
SETTINGS join_use_nulls = 1;

-- 7. Similar payments: candidates for investigation, NOT proven duplicates.
-- Several tickets bought together may legitimately have identical values.
-- Do not drop these rows without a real ticket/transaction identifier.
SELECT session_date, part_of_day, cinema_id, cinema_room_id,
    client_id, pay_at, type_pay, count() AS similar_rows
FROM alpha_cinema.films_payments
GROUP BY session_date, part_of_day, cinema_id, cinema_room_id,
    client_id, pay_at, type_pay
HAVING similar_rows > 1
ORDER BY similar_rows DESC;

-- 8. Date boundaries by payment and session calendar.
SELECT min(pay_at) AS first_payment, max(pay_at) AS last_payment,
    min(session_date) AS first_sold_session, max(session_date) AS last_sold_session,
    countIf(toDate(pay_at) != toDate(session_date)) AS different_calendar_dates
FROM alpha_cinema.films_payments;

SELECT toStartOfMonth(toDate(pay_at)) AS payment_month,
    min(toDate(pay_at)) AS first_payment_day,
    max(toDate(pay_at)) AS last_payment_day,
    uniqExact(toDate(pay_at)) AS days_with_sales,
    count() AS ticket_rows
FROM alpha_cinema.films_payments
GROUP BY payment_month ORDER BY payment_month;

SELECT toStartOfMonth(session_date) AS session_month,
    min(session_date) AS first_scheduled_day,
    max(session_date) AS last_scheduled_day,
    uniqExact(session_date) AS scheduled_days,
    count() AS scheduled_sessions
FROM alpha_cinema.schedule
GROUP BY session_month ORDER BY session_month;

-- Min/max and sale-day counts alone do not prove extraction completeness.
-- Confirm extraction boundaries and expected operating days with the source.

-- 9. Zero-sale scheduled sessions (must remain in the occupancy denominator).
SELECT count() AS scheduled_sessions,
    countIf(isNull(p.tickets_sold)) AS zero_sale_sessions,
    sum(coalesce(p.tickets_sold, 0)) AS matched_ticket_rows
FROM alpha_cinema.schedule AS s
LEFT JOIN
(
    SELECT session_date, part_of_day, cinema_id, cinema_room_id,
        count() AS tickets_sold
    FROM alpha_cinema.films_payments
    GROUP BY session_date, part_of_day, cinema_id, cinema_room_id
) AS p
    ON s.session_date = p.session_date AND s.part_of_day = p.part_of_day
    AND s.cinema_id = p.cinema_id AND s.cinema_room_id = p.cinema_room_id
SETTINGS join_use_nulls = 1;

-- After exporting the proposed session query, reconcile:
-- * session row count with schedule row count;
-- * SUM(session_tickets_sold) with matched_ticket_rows from check 9;
-- * SUM(session_revenue) with reconstructed_revenue only if no unmatched
--   payments, invalid prices or duplicate dimension/schedule keys remain;
-- * the same unfiltered totals in pandas and DataLens.
