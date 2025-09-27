CREATE TABLE IF NOT EXISTS users (
  tg_id BIGINT PRIMARY KEY,
  first_name TEXT,
  username TEXT,
  created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS favorites (
  id SERIAL PRIMARY KEY,
  tg_id BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
  base VARCHAR(10) NOT NULL,
  target VARCHAR(10) NOT NULL,
  UNIQUE (tg_id, base, target)
);