-- Адаптация исходного SQL учебного проекта.
-- Исправление: month определяется по дате сеанса, а не дате оплаты.
-- Запрос включён для прозрачности метода; на исходной базе эта версия не исполнена.
-- Одна строка соответствует исходной строке films_payments.
-- Основа от продаж не включает сеансы без продаж.
-- Уникальность ключей расписания/справочников должна быть проверена до JOIN.
WITH base_joined AS(
	SELECT 
		s.film_id AS film_id,
		f.price_ticket AS price_ticket,
		f.film_name AS film_name,
		fp.session_date AS session_date,
		fp.part_of_day AS part_of_day,
		fp.client_id AS client_id,
		fp.type_pay AS type_pay,
		fp.pay_at AS pay_at,
		fp.cinema_id AS cinema_id,
		fp.cinema_room_id AS cinema_room_id,
		dcr.count_seats AS count_seats,
		dcr.is_vip AS is_vip,
		dcr.is_3d AS is_3d,
		c.discount_perc AS discount_perc,
		(f.price_ticket*c.discount_perc)/100 AS discount_amount,
		f.price_ticket - (f.price_ticket*c.discount_perc)/100 AS ticket_price_final,
		toMonth(fp.session_date) AS month,
		multiIf(
			dcr.is_vip = 1 AND dcr.is_3d = 1, 'VIP+3D', 
			dcr.is_vip = 0 AND dcr.is_3d = 1, '3D', 
			dcr.is_vip = 1 AND dcr.is_3d = 0, 'VIP',
			'Standart') cinema_room_type,
		concat(
			toString(fp.session_date),' ',
			toString(fp.part_of_day),' ',
			toString(fp.cinema_id),' ',
			toString(fp.cinema_room_id)) AS session_key --технический столбец
	FROM alpha_cinema.films_payments fp 
	LEFT JOIN alpha_cinema.schedule s 
		ON fp.session_date = s.session_date 
		AND fp.part_of_day = s.part_of_day 
		AND fp.cinema_id = s.cinema_id 
		AND fp.cinema_room_id = s.cinema_room_id
	LEFT JOIN alpha_cinema.films f 
		ON s.film_id = f.film_id 
	LEFT JOIN alpha_cinema.clients c 
		ON c.client_id = fp.client_id 
	LEFT JOIN alpha_cinema.dict_cinema_rooms dcr 
		ON fp.cinema_id = dcr.cinema_id
		AND fp.cinema_room_id = dcr.cinema_room_id),
base_enriched AS(
	SELECT
		*,
		ROW_NUMBER() OVER(PARTITION BY client_id ORDER BY pay_at) AS client_purchase_number,
		COUNT() OVER(PARTITION BY session_key) AS session_tickets_sold,
		SUM(ticket_price_final) OVER(PARTITION BY session_key) AS session_revenue
	FROM base_joined bj)
SELECT *, ROUND(session_tickets_sold/count_seats*100,2) AS occupancy_rate
FROM base_enriched
ORDER BY film_id;

