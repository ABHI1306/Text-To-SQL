CREATE SCHEMA ecommerce;
CREATE SCHEMA human_resources;

CREATE TABLE ecommerce.customers (
    customer_id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE ecommerce.addresses (
    address_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES ecommerce.customers(customer_id),
    city TEXT NOT NULL,
    region TEXT NOT NULL,
    country TEXT NOT NULL
);
CREATE TABLE ecommerce.categories (
    category_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE ecommerce.products (
    product_id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES ecommerce.categories(category_id),
    name TEXT NOT NULL,
    unit_price NUMERIC(10, 2) NOT NULL CHECK (unit_price >= 0),
    active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE ecommerce.orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES ecommerce.customers(customer_id),
    ordered_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    region TEXT NOT NULL
);
CREATE TABLE ecommerce.order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES ecommerce.orders(order_id),
    product_id INTEGER NOT NULL REFERENCES ecommerce.products(product_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(10, 2) NOT NULL
);
CREATE TABLE ecommerce.payments (
    payment_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES ecommerce.orders(order_id),
    paid_at TIMESTAMPTZ,
    amount NUMERIC(10, 2) NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE ecommerce.shipments (
    shipment_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES ecommerce.orders(order_id),
    shipped_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    carrier TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE ecommerce.reviews (
    review_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES ecommerce.customers(customer_id),
    product_id INTEGER NOT NULL REFERENCES ecommerce.products(product_id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE ecommerce.coupons (
    coupon_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    discount_percent NUMERIC(5, 2) NOT NULL CHECK (discount_percent BETWEEN 0 AND 100),
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE ecommerce.order_coupons (
    order_id INTEGER NOT NULL REFERENCES ecommerce.orders(order_id),
    coupon_id INTEGER NOT NULL REFERENCES ecommerce.coupons(coupon_id),
    PRIMARY KEY (order_id, coupon_id)
);
CREATE TABLE ecommerce.inventory (
    product_id INTEGER PRIMARY KEY REFERENCES ecommerce.products(product_id),
    quantity_on_hand INTEGER NOT NULL CHECK (quantity_on_hand >= 0),
    warehouse_region TEXT NOT NULL
);

INSERT INTO ecommerce.customers VALUES
    (1, 'Asha', 'Rao', 'asha@example.test', 'West', now() - interval '90 days'),
    (2, 'Ravi', 'Shah', 'ravi@example.test', 'South', now() - interval '45 days'),
    (3, 'Mina', 'Das', 'mina@example.test', 'West', now() - interval '12 days');
INSERT INTO ecommerce.addresses VALUES
    (1, 1, 'Pune', 'West', 'IN'),
    (2, 2, 'Bengaluru', 'South', 'IN'),
    (3, 3, 'Mumbai', 'West', 'IN');
INSERT INTO ecommerce.categories VALUES (1, 'Books'), (2, 'Home');
INSERT INTO ecommerce.products VALUES
    (1, 1, 'Data Systems', 799.00, TRUE),
    (2, 2, 'Desk Lamp', 1299.00, TRUE),
    (3, 1, 'SQL Patterns', 599.00, TRUE);
INSERT INTO ecommerce.orders VALUES
    (101, 1, now() - interval '20 days', 'delivered', 'West'),
    (102, 2, now() - interval '8 days', 'shipped', 'South'),
    (103, 1, now() - interval '2 days', 'paid', 'West'),
    (104, 3, now() - interval '1 day', 'paid', 'West');
INSERT INTO ecommerce.order_items VALUES
    (1, 101, 1, 1, 799.00),
    (2, 101, 3, 2, 599.00),
    (3, 102, 2, 1, 1299.00),
    (4, 103, 1, 1, 799.00),
    (5, 104, 2, 2, 1299.00);
INSERT INTO ecommerce.payments VALUES
    (1, 101, now() - interval '20 days', 1997.00, 'card', 'paid'),
    (2, 102, now() - interval '8 days', 1299.00, 'upi', 'paid'),
    (3, 103, now() - interval '2 days', 799.00, 'card', 'paid'),
    (4, 104, now() - interval '1 day', 2598.00, 'upi', 'paid');
INSERT INTO ecommerce.shipments VALUES
    (1, 101, now() - interval '19 days', now() - interval '16 days', 'LocalShip', 'delivered'),
    (2, 102, now() - interval '7 days', NULL, 'ParcelCo', 'in_transit');
INSERT INTO ecommerce.reviews VALUES
    (1, 1, 1, 5, now() - interval '15 days'),
    (2, 2, 2, 4, now() - interval '5 days'),
    (3, 3, 2, 5, now() - interval '1 day');
INSERT INTO ecommerce.coupons VALUES
    (1, 'WELCOME10', 10, now() - interval '30 days', now() + interval '30 days');
INSERT INTO ecommerce.order_coupons VALUES (101, 1);
INSERT INTO ecommerce.inventory VALUES (1, 40, 'West'), (2, 18, 'South'), (3, 70, 'West');

CREATE TABLE human_resources.departments (
    department_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE human_resources.employees (
    employee_id INTEGER PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES human_resources.departments(department_id),
    manager_id INTEGER REFERENCES human_resources.employees(employee_id),
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    title TEXT NOT NULL,
    hire_date DATE NOT NULL,
    region TEXT NOT NULL
);
CREATE TABLE human_resources.salary_history (
    employee_id INTEGER NOT NULL REFERENCES human_resources.employees(employee_id),
    valid_from DATE NOT NULL,
    annual_salary NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (employee_id, valid_from)
);
CREATE TABLE human_resources.leave_requests (
    leave_id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES human_resources.employees(employee_id),
    starts_on DATE NOT NULL,
    ends_on DATE NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE human_resources.performance_reviews (
    review_id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES human_resources.employees(employee_id),
    review_year INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5)
);

INSERT INTO human_resources.departments VALUES
    (1, 'Engineering'), (2, 'Sales'), (3, 'People');
INSERT INTO human_resources.employees VALUES
    (1001, 1, NULL, 'Neel', 'Kapoor', 'Engineering Manager', DATE '2021-03-15', 'West'),
    (1002, 1, 1001, 'Ira', 'Mehta', 'Data Engineer', DATE '2023-07-01', 'West'),
    (1003, 2, NULL, 'Dev', 'Nair', 'Sales Lead', DATE '2020-11-10', 'South'),
    (1004, 3, NULL, 'Sara', 'Khan', 'HR Partner', DATE '2022-01-20', 'West');
INSERT INTO human_resources.salary_history VALUES
    (1001, DATE '2025-01-01', 1800000),
    (1002, DATE '2025-01-01', 1250000),
    (1003, DATE '2025-01-01', 1600000),
    (1004, DATE '2025-01-01', 1100000);
INSERT INTO human_resources.leave_requests VALUES
    (1, 1002, DATE '2026-01-12', DATE '2026-01-14', 'approved'),
    (2, 1004, DATE '2026-02-02', DATE '2026-02-03', 'pending');
INSERT INTO human_resources.performance_reviews VALUES
    (1, 1001, 2025, 4), (2, 1002, 2025, 5), (3, 1003, 2025, 4), (4, 1004, 2025, 4);

\ir grants.sql
