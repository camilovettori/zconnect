import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect

from .db import engine
from . import models
from .api import routes

# Tables managed only by Alembic migrations are never created implicitly at startup, so
# deploying code cannot silently run a schema change. export_events comes from
# migration 0002_export_events.
MIGRATION_ONLY_TABLES = {"export_events"}

models.Base.metadata.create_all(
    bind=engine,
    tables=[table for table in models.Base.metadata.sorted_tables if table.name not in MIGRATION_ONLY_TABLES],
)

if not inspect(engine).has_table("export_events"):
    logging.getLogger(__name__).critical(
        "export_events table is missing: run the approved migration 0002_export_events. "
        "Invoice syncs and resets will fail closed until it exists."
    )

app = FastAPI(title="Unify to Zoho Draft Connector", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes.router, prefix="/api")


@app.get("/")
async def root():
    return {"message": "Unify to Zoho Draft Connector API"}
