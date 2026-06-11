# WhatsApp Meal Survey API

Flask backend for sending meal satisfaction surveys via WhatsApp. Deployed on Render (free tier) with Supabase as the database.

## Features

- **3-button WhatsApp survey** — Very Good / Satisfactory / Not Acceptable
- **"Not Acceptable" flow** — user types free-text feedback
- **One response per user per date** — UNIQUE constraint in Supabase
- **Per-date meal availability** — only availed users get surveyed
- **Auto-send via cron-job.org** — works on Render free tier
- **Server health monitoring** — ping endpoint + uptime badge in UI
- **Server-side date filtering** — saves bandwidth on free tier

## Render Free Tier — Server Wake-Up Strategy

Render free tier sleeps after 15 minutes of inactivity. When the server is asleep, the first HTTP request wakes it (~30-60s cold start). To ensure the daily survey cron runs reliably:

**Set up TWO cron jobs on cron-job.org (free):**

| Job | Time (PKT) | Time (UTC) | URL | Purpose |
|-----|-----------|------------|-----|---------|
| 1 | 2:25 PM | 09:25 | `/api/health/ping` | Wake the server |
| 2 | 2:30 PM | 09:30 | `/api/cron/daily-survey?secret=YOUR_CRON_SECRET` | Send surveys |

The 5-minute gap ensures the server is fully awake when Job 2 fires.

## Setup

### 1. Supabase Database

Run `supabase_schema.sql` in the Supabase SQL Editor to create tables and indexes.

### 2. Deploy to Render

1. Push this repo to GitHub
2. Create a new Web Service on Render, connect the repo
3. Set environment variables:
   - `WHATSAPP_TOKEN` — from Meta Business Suite
   - `WHATSAPP_PHONE_NUMBER_ID` — from Meta app dashboard
   - `WHATSAPP_VERIFY_TOKEN` — any string you choose (for webhook verification)
   - `SUPABASE_URL` — your Supabase project URL
   - `SUPABASE_SERVICE_KEY` — your Supabase service_role key
   - `CRON_SECRET` — any secret string (protects the cron endpoint)

### 3. Configure WhatsApp Webhook

In Meta app dashboard:
- Callback URL: `https://your-app.onrender.com/webhook`
- Verify token: the `WHATSAPP_VERIFY_TOKEN` you set above

### 4. Set Up cron-job.org

Create a free account at [cron-job.org](https://cron-job.org) and add two jobs:

**Job 1 — Wake Server**
- URL: `https://your-app.onrender.com/api/health/ping`
- Schedule: Every day at 09:25 UTC
- Method: GET

**Job 2 — Send Surveys**
- URL: `https://your-app.onrender.com/api/cron/daily-survey?secret=YOUR_CRON_SECRET`
- Schedule: Every day at 09:30 UTC
- Method: GET

## API Endpoints

### Health / Ping
- `GET /api/health/ping` — Ultra-lightweight, wakes server, returns `{"status":"awake"}`
- `GET /api/health/status` — Server uptime, last ping time

### Webhook
- `GET /webhook` — WhatsApp verification
- `POST /webhook` — Receive WhatsApp messages

### Survey
- `POST /api/survey/send` — Send surveys manually (JSON body)
- `GET /api/cron/daily-survey?secret=XXX` — Cron-triggered auto-send

### Scheduler
- `GET /api/scheduler/status` — Cron job info, last run, server health
- `GET /api/scheduler/logs` — Recent auto-send logs

### Users
- `GET /api/users` — List all users
- `POST /api/users` — Add user
- `PATCH /api/users/<id>` — Update user
- `DELETE /api/users/<id>` — Delete user

### Meal Availability
- `GET /api/mealavail/<date>` — Get availability for date
- `POST /api/mealavail` — Set single user availability
- `POST /api/mealavail/bulk` — Set bulk availability

### UI Data
- `GET /api/ui/recent?date=YYYY-MM-DD` — Recent responses (server-side date filter)
- `GET /api/ui/surveys` — Survey history
- `DELETE /api/ui/reset/<phone>` — Reset user responses

## Bandwidth Optimizations (for Render free tier)

- **Server-side date filtering** — `/api/ui/recent?date=...` only returns data for one date
- **30-second auto-refresh** — reduced from 15s to save bandwidth
- **Lightweight ping endpoint** — minimal JSON response, no DB queries
- **Tab-based lazy loading** — only the active tab fetches data
- **No unnecessary polling** — scheduler status fetched once on load
