# Zconnect

A full-stack integration system built to automate invoice creation between Unify Ordering and Zoho Books.

---

## 🔗 Overview

Zconnect is a real-world business integration project designed to eliminate manual work in the invoicing process.

Many small businesses use different systems for orders and accounting. This creates repetitive work, delays, and a high risk of human error.

This platform connects **Unify Ordering** with **Zoho Books**, allowing orders to be fetched, reviewed, and converted into draft invoices automatically.

Instead of manually copying data, everything can be synced in a few clicks.

---

## 🚀 Features

* Fetch delivery orders from Unify
* Filter orders by delivery date
* Convert orders into draft invoices in Zoho
* Customer and product mapping
* Multi-tax support (0%, 13.5%, 23%)
* Prevent duplicate invoice creation
* Error handling and fallback logic
* Clean review workflow before syncing
* Sync history tracking

---

## 🛠 Tech Stack

**Frontend**

* Next.js
* TypeScript
* Tailwind CSS

**Backend**

* Python
* FastAPI

**Database**

* PostgreSQL

**Integrations**

* Unify Ordering API
* Zoho Books API

---

## ⚙️ How It Works

1. Select a delivery date or range
2. Fetch orders from Unify
3. Review the data in the interface
4. Sync selected orders
5. Draft invoices are created in Zoho automatically

---

## 🧱 Architecture

```
Frontend (Next.js)
        ↓
Backend (FastAPI)
        ↓
Unify API + Zoho API
```

The frontend handles the user interaction, while the backend manages data transformation, validation, and integration between systems.

---

## 💡 Problem Solved

Before Zconnect:

* Manual data entry
* Repeated work
* High chance of errors
* No proper tracking

With Zconnect:

* Automated workflow
* Faster invoicing
* More reliable data
* Scalable process

---

## 🧠 Key Learnings

This project helped me develop practical experience in:

* API integration
* Data transformation between systems
* Backend architecture with FastAPI
* Full-stack application structure
* Handling real-world edge cases
* Building software for business operations

---

## ⚠️ Challenges

* Different data structures between Unify and Zoho
* Missing or inconsistent API data
* Handling multiple tax rates correctly
* Preventing duplicate invoices
* Making the sync process reliable

---

## 📦 Project Structure

```
/
├─ backend/
├─ frontend/
├─ docs/
├─ README.md
├─ .env.example
└─ apiv1.yaml
```

---

## ▶️ Local Setup

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy ..\.env.example .env
uvicorn app.main:app --reload
```

---

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open:

```
http://localhost:3000
```

---

## 🚧 Status

This project is functional and designed for real-world usage, with core integration features implemented and tested.

---

## 🎯 Future Improvements

* Scheduled automatic sync
* Dashboard analytics
* Multi-tenant SaaS version
* Additional integrations
* Improved reporting

---

## 👤 Author

Camilo Vettori
Founder of **Ziffera**

