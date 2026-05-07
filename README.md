# Zconnect — Unify to Zoho Invoice Automation  

"Simple digital automation for real businesses"

Zconnect is a production-style integration platform that connects Unify Ordering with Zoho Books to automate invoice creation. It is designed for cafés, bakeries, and small businesses that need a reliable way to turn delivery orders into draft invoices without manual re-entry, copy-paste errors, or duplicated work.

## Overview

Modern small businesses often manage orders in one system and accounting in another. That split creates friction: staff must manually copy customer details, line items, tax values, and totals from Unify into Zoho Books. Zconnect removes that repetitive work by syncing order data from Unify and creating Zoho draft invoices automatically.

The result is a faster, cleaner workflow that reduces human error, preserves auditability, and gives operators a simple review-and-sync process instead of a spreadsheet-driven one.

## Key Features

- Fetch orders from Unify
- Smart order filtering by delivery date
- Product name resolution without raw numeric IDs
- Customer mapping
- Automatic Zoho draft invoice creation
- Tax handling for 0%, 13.5%, and 23%
- Sync history tracking
- Error handling and retry-safe architecture
- Fallback system that never breaks invoice creation

## Architecture

Frontend → Backend → Unify API → Zoho API

The frontend provides the operator experience, the backend handles orchestration and persistence, and the external APIs provide source order data and accounting invoice creation.

## Tech Stack

- FastAPI
- Next.js
- TypeScript
- Tailwind
- PostgreSQL
- REST APIs

## Screenshots

![Dashboard](./docs/dashboard.png)
![Sync Page](./docs/sync.png)
![History](./docs/history.png)

## How It Works

1. Select a delivery date range.
2. Fetch orders from Unify.
3. Review the preview and order details.
4. Sync selected orders to Zoho.
5. Draft invoices are created automatically.

## Challenges Solved

Zconnect was built around the kinds of integration problems that appear in real production systems:

- Handling incomplete API data, including missing product names
- Mapping external systems with different schemas
- Tax resolution logic for different product types
- Preventing duplicate invoices
- Building fallback-safe integrations
- Handling API failures, including Zoho errors and authorization issues

## What I Learned

- API integration at scale
- Data normalization
- Backend architecture
- Error handling strategies
- Building production-ready systems

## Future Improvements

- Auto-sync scheduler
- Multi-tenant SaaS version
- Dashboard analytics
- Stripe billing integration

## Author

Camilo (Ziffera)

## Project Structure

```text
/
├─ backend/
├─ frontend/
├─ docs/
├─ README.md
├─ .env.example
└─ .gitignore
```

## Setup

### Backend

1. `cd backend`
2. Create a virtual environment: `python -m venv .venv`
3. Activate it on Windows: `.venv\Scripts\activate`
4. Install dependencies: `pip install -r requirements.txt`
5. Copy the environment template: `copy ..\.env.example .env`
6. Fill in your secrets in `.env`
7. Run the API: `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`

### Frontend

1. `cd frontend`
2. Install dependencies: `npm install`
3. Start the app: `npm run dev`
4. Open `http://localhost:3000`

## Environment Variables

Use the provided `.env.example` as a starting point for local development.

## Git Commands

```bash
git init
git add .
git commit -m "Initial commit - Zconnect integration system"
git branch -M main
git remote add origin <repo-url>
git push -u origin main
```

## Footer

Developed by Ziffera  
www.ziffera.ie  
"Simple digital solutions for local businesses"
