import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect

from .config import settings
from .db import engine
from . import models
from .api import routes
from .security import configured_secrets

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

if not configured_secrets():
    logging.getLogger(__name__).critical(
        "ZCONNECT_BACKEND_SECRET is missing or shorter than 32 characters: every /api request will be rejected."
    )

# Interactive docs publish the full API map; only enable them deliberately (e.g. locally).
_docs_enabled = settings.ZCONNECT_ENABLE_API_DOCS
app = FastAPI(
    title="Unify to Zoho Draft Connector",
    version="0.1.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# CORS is not an authentication mechanism (server-to-server calls ignore it); every /api
# route requires a signed identity from the Next.js server (app/security.py). Browsers are
# not expected to call this API directly, so credentials are not allowed cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(routes.router, prefix="/api")


@app.get("/")
async def root():
    return {"message": "Unify to Zoho Draft Connector API"}
