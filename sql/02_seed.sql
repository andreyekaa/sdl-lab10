\connect event_hub

INSERT INTO venues (name, city, capacity, venue_type) VALUES
    ('Northern Stage', 'Moscow', 1800, 'arena'),
    ('Signal Hall', 'Saint Petersburg', 900, 'club'),
    ('River Sound Garden', 'Kazan', 3500, 'open_air');

INSERT INTO events (venue_id, title, genre, starts_at, status, base_price) VALUES
    (1, 'Neon Skyline', 'synthwave', '2026-05-15 20:00:00', 'scheduled', 3200.00),
    (2, 'Acoustic Letters', 'indie', '2026-05-22 19:30:00', 'scheduled', 2400.00),
    (3, 'Summer Pulse', 'electronic', '2026-06-05 18:00:00', 'scheduled', 4100.00);

INSERT INTO ticket_types (event_id, title, price, quota) VALUES
    (1, 'Standard', 3200.00, 1000),
    (1, 'VIP Balcony', 5700.00, 180),
    (2, 'Parterre', 2400.00, 600),
    (3, 'Early Bird', 3600.00, 450),
    (3, 'Backstage', 7900.00, 80);

INSERT INTO customers (full_name, email, loyalty_tier) VALUES
    ('Anna Volkova', 'anna.volkova@example.com', 'gold'),
    ('Ilya Sokolov', 'ilya.sokolov@example.com', 'standard'),
    ('Maria Orlova', 'maria.orlova@example.com', 'silver');

INSERT INTO orders (customer_id, sales_channel, status) VALUES
    (1, 'mobile_app', 'paid'),
    (2, 'web', 'reserved');

INSERT INTO order_items (order_id, ticket_type_id, quantity, unit_price) VALUES
    (1, 2, 2, 5700.00),
    (1, 1, 1, 3200.00),
    (2, 4, 3, 3600.00);
