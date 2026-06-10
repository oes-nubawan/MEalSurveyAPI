-- Sample Data for Meal Survey
-- Run this AFTER supabase_schema.sql in the Supabase SQL Editor

-- ═══════════════════════════════════════════════════════════════════════
--  USERS — 6 sample users
-- ═══════════════════════════════════════════════════════════════════════
INSERT INTO tbl_users (name, phoneno, availstatus) VALUES
    ('Zayan', '923128281449', true),
    ('Ahmed', '923377078957', true),
    ('Fatima', '923001234567', true),
    ('Ali', '923009876543', false),
    ('Sara', '923129876543', true),
    ('Hassan', '923376543210', false);

-- ═══════════════════════════════════════════════════════════════════════
--  MEALS — 3 sample meals
-- ═══════════════════════════════════════════════════════════════════════
INSERT INTO tbl_meal (meal_date, meal_name) VALUES
    ('2026-06-08', 'Chicken Biryani'),
    ('2026-06-09', 'Dal Chawal'),
    ('2026-06-10', 'Murgh Channa');

-- ═══════════════════════════════════════════════════════════════════════
--  RESPONSES — sample responses for June 8 & 9
--  (Zayan and Ahmed gave feedback, Fatima pending, Ali skipped)
-- ═══════════════════════════════════════════════════════════════════════

-- June 8: Chicken Biryani
INSERT INTO tbl_usersresponse (user_id, meal_date, meal_name, response_status, user_response, remarks, response_datetime) VALUES
    ((SELECT id FROM tbl_users WHERE phoneno = '923128281449'), '2026-06-08', 'Chicken Biryani', 'completed', 'very_good', NULL, '2026-06-08T14:30:00Z'),
    ((SELECT id FROM tbl_users WHERE phoneno = '923377078957'), '2026-06-08', 'Chicken Biryani', 'completed', 'satisfactory', NULL, '2026-06-08T15:10:00Z'),
    ((SELECT id FROM tbl_users WHERE phoneno = '923001234567'), '2026-06-08', 'Chicken Biryani', 'completed', 'not_ok', 'Rice was undercooked and bland', '2026-06-08T14:45:00Z'),
    ((SELECT id FROM tbl_users WHERE phoneno = '923129876543'), '2026-06-08', 'Chicken Biryani', 'completed', 'good', NULL, '2026-06-08T16:00:00Z');

-- June 9: Dal Chawal
INSERT INTO tbl_usersresponse (user_id, meal_date, meal_name, response_status, user_response, remarks, response_datetime) VALUES
    ((SELECT id FROM tbl_users WHERE phoneno = '923128281449'), '2026-06-09', 'Dal Chawal', 'completed', 'good', NULL, '2026-06-09T13:20:00Z'),
    ((SELECT id FROM tbl_users WHERE phoneno = '923377078957'), '2026-06-09', 'Dal Chawal', 'completed', 'not_ok', 'Pani pani tha, no taste', '2026-06-09T13:35:00Z'),
    ((SELECT id FROM tbl_users WHERE phoneno = '923001234567'), '2026-06-09', 'Dal Chawal', 'completed', 'satisfactory', NULL, '2026-06-09T14:00:00Z');

-- June 10: Murgh Channa — mix of pending, rated (waiting remarks), completed
INSERT INTO tbl_usersresponse (user_id, meal_date, meal_name, response_status, user_response, remarks, response_datetime) VALUES
    ((SELECT id FROM tbl_users WHERE phoneno = '923128281449'), '2026-06-10', 'Murgh Channa', 'completed', 'not_ok', 'Pani pani', '2026-06-10T08:48:00Z'),
    ((SELECT id FROM tbl_users WHERE phoneno = '923377078957'), '2026-06-10', 'Murgh Channa', 'pending', NULL, NULL, NULL),
    ((SELECT id FROM tbl_users WHERE phoneno = '923001234567'), '2026-06-10', 'Murgh Channa', 'rated', 'not_ok', NULL, NULL),
    ((SELECT id FROM tbl_users WHERE phoneno = '923129876543'), '2026-06-10', 'Murgh Channa', 'completed', 'very_good', NULL, '2026-06-10T09:10:00Z');
