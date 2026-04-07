from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import routes

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
