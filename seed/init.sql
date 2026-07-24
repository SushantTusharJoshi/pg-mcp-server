-- Bookstore seed data for pg-mcp-server

CREATE EXTENSION IF NOT EXISTS pg_trgm;

BEGIN;

-- Tables

CREATE TABLE authors (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    birth_year INTEGER,
    nationality TEXT
);

CREATE TABLE genres (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT
);

CREATE TABLE books (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    genre_id INTEGER NOT NULL REFERENCES genres(id),
    published_year INTEGER NOT NULL,
    isbn TEXT UNIQUE,
    pages INTEGER,
    price NUMERIC(6,2),
    rating NUMERIC(2,1) CHECK (rating >= 0 AND rating <= 5),
    in_stock BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_books_author ON books(author_id);
CREATE INDEX idx_books_genre ON books(genre_id);
CREATE INDEX idx_books_title_trgm ON books USING gin (title gin_trgm_ops);

-- Seed authors

INSERT INTO authors (name, birth_year, nationality) VALUES
    ('Chinua Achebe',        1930, 'Nigerian'),
    ('Ursula K. Le Guin',    1929, 'American'),
    ('Gabriel García Márquez', 1927, 'Colombian'),
    ('Toni Morrison',        1931, 'American'),
    ('Haruki Murakami',      1949, 'Japanese'),
    ('Chimamanda Ngozi Adichie', 1977, 'Nigerian'),
    ('Jorge Luis Borges',    1899, 'Argentine'),
    ('Octavia Butler',       1947, 'American');

-- Seed genres

INSERT INTO genres (name, description) VALUES
    ('Literary Fiction',  'Character-driven narratives exploring the human condition'),
    ('Science Fiction',   'Speculative fiction grounded in scientific concepts'),
    ('Magical Realism',   'Realistic narrative blended with magical elements'),
    ('Short Stories',     'Collections of brief fictional works'),
    ('Fantasy',           'Fiction set in imaginary worlds with supernatural elements');

-- Seed books

INSERT INTO books (title, author_id, genre_id, published_year, isbn, pages, price, rating, in_stock) VALUES
    ('Things Fall Apart',                    1, 1, 1958, '978-0385474542', 209,  12.99, 4.2, true),
    ('Arrow of God',                         1, 1, 1964, '978-0385014809', 230,  14.99, 4.0, true),
    ('The Left Hand of Darkness',            2, 2, 1969, '978-0441478125', 304,  15.99, 4.4, true),
    ('The Dispossessed',                     2, 2, 1974, '978-0061054884', 387,  16.99, 4.5, true),
    ('A Wizard of Earthsea',                 2, 5, 1968, '978-0547722023', 183,  11.99, 4.3, true),
    ('One Hundred Years of Solitude',        3, 3, 1967, '978-0060883287', 417,  16.99, 4.6, true),
    ('Love in the Time of Cholera',          3, 3, 1985, '978-0307389732', 348,  15.99, 4.3, false),
    ('Beloved',                              4, 1, 1987, '978-1400033416', 324,  14.99, 4.4, true),
    ('Song of Solomon',                      4, 1, 1977, '978-1400033423', 337,  15.99, 4.3, true),
    ('The Bluest Eye',                       4, 1, 1970, '978-0307278463', 206,  13.99, 4.1, true),
    ('Norwegian Wood',                       5, 1, 1987, '978-0375704024', 296,  14.99, 4.0, true),
    ('Kafka on the Shore',                   5, 1, 2002, '978-1400079278', 467,  16.99, 4.2, false),
    ('1Q84',                                 5, 2, 2009, '978-0307476463', 925,  19.99, 3.9, true),
    ('Half of a Yellow Sun',                 6, 1, 2006, '978-1400095209', 433,  15.99, 4.5, true),
    ('Americanah',                           6, 1, 2013, '978-0307455925', 477,  16.99, 4.4, true),
    ('Purple Hibiscus',                      6, 1, 2003, '978-1616202415', 307,  14.99, 4.2, true),
    ('Ficciones',                            7, 4, 1944, '978-0802130303', 174,  13.99, 4.5, true),
    ('The Aleph and Other Stories',          7, 4, 1949, '978-0142437889', 187,  13.99, 4.3, false),
    ('Kindred',                              8, 2, 1979, '978-0807083697', 264,  14.99, 4.6, true),
    ('Parable of the Sower',                 8, 2, 1993, '978-1538732182', 345,  15.99, 4.5, true);

-- Read-only role for the MCP server

CREATE ROLE mcp_readonly WITH LOGIN PASSWORD 'mcp_readonly';
GRANT CONNECT ON DATABASE bookstore TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_readonly;

COMMIT;
