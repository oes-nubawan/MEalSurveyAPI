-- Meal Survey — Supabase Schema
-- Run this in the Supabase SQL Editor

-- ═══════════════════════════════════════════════════════════════════════
--  MEALS TABLE
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tbl_meal (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    meal_date DATE NOT NULL,
    meal_name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(meal_date, meal_name)
);

-- ═══════════════════════════════════════════════════════════════════════
--  USERS TABLE
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tbl_users (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT NOT NULL,
    phoneno TEXT NOT NULL UNIQUE,
    availstatus BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════════════
--  USER RESPONSES TABLE
--  One row per user per meal_date — UNIQUE constraint enforces this.
--  response_status: 'pending' → 'rated' → 'completed'
--    pending  = survey sent, waiting for rating
--    rated    = rating given (not_ok), waiting for remarks
--    completed = fully done (positive rating OR not_ok + remarks)
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tbl_usersresponse (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES tbl_users(id) ON DELETE CASCADE,
    meal_date DATE NOT NULL,
    meal_name TEXT NOT NULL,
    response_status TEXT DEFAULT 'pending',
    user_response TEXT,
    remarks TEXT,
    response_datetime TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, meal_date)
);

-- ═══════════════════════════════════════════════════════════════════════
--  INDEXES
-- ═══════════════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_users_phone ON tbl_users(phoneno);
CREATE INDEX IF NOT EXISTS idx_users_avail ON tbl_users(availstatus);
CREATE INDEX IF NOT EXISTS idx_response_user_date ON tbl_usersresponse(user_id, meal_date);
CREATE INDEX IF NOT EXISTS idx_response_status ON tbl_usersresponse(response_status);

-- ═══════════════════════════════════════════════════════════════════════
--  ENABLE RLS (Row Level Security)
-- ═══════════════════════════════════════════════════════════════════════
ALTER TABLE tbl_meal ENABLE ROW LEVEL SECURITY;
ALTER TABLE tbl_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE tbl_usersresponse ENABLE ROW LEVEL SECURITY;

-- Allow service_role full access (backend uses service key)
CREATE POLICY "Service role full access" ON tbl_meal FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON tbl_users FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON tbl_usersresponse FOR ALL USING (true) WITH CHECK (true);
