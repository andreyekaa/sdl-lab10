from decimal import Decimal
from typing import Annotated

import psycopg
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


app = FastAPI(
    title="Event Hub API",
    version="1.0.0",
    description="HTTP API for DAST testing with OWASP ZAP",
)


DB_CONFIG = {
    "host": "db",
    "port": 5432,
    "dbname": "event_hub",
    "user": "app_user",
    "password": "app_pass",
    "connect_timeout": 5,
}


class VenueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    capacity: int = Field(gt=0, le=1_000_000)
    venue_type: str = Field(default="club", min_length=1, max_length=50)


class CustomerCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    loyalty_tier: str = Field(default="standard", min_length=1, max_length=50)


class EventCreate(BaseModel):
    venue_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    genre: str = Field(min_length=1, max_length=100)
    starts_at: str = Field(min_length=10, max_length=40)
    status: str = Field(default="scheduled", min_length=1, max_length=50)
    base_price: Decimal = Field(ge=0)


def get_connection():
    return psycopg.connect(**DB_CONFIG)


@app.get("/")
def root():
    return {
        "service": "Event Hub API",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.get("/health")
def health():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database is unavailable")


@app.get("/venues")
def list_venues(
    city: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if city:
                cur.execute(
                    """
                    SELECT id, name, city, capacity, venue_type
                    FROM venues
                    WHERE city = %s
                    ORDER BY id
                    LIMIT %s;
                    """,
                    (city, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, name, city, capacity, venue_type
                    FROM venues
                    ORDER BY id
                    LIMIT %s;
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "name": row[1],
            "city": row[2],
            "capacity": row[3],
            "venue_type": row[4],
        }
        for row in rows
    ]


@app.post("/venues", status_code=201)
def create_venue(payload: VenueCreate):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO venues (name, city, capacity, venue_type)
                VALUES (%s, %s, %s, %s)
                RETURNING id, name, city, capacity, venue_type;
                """,
                (payload.name, payload.city, payload.capacity, payload.venue_type),
            )
            row = cur.fetchone()
        conn.commit()

    return {
        "id": row[0],
        "name": row[1],
        "city": row[2],
        "capacity": row[3],
        "venue_type": row[4],
    }


@app.get("/events")
def list_events(
    genre: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if genre:
                cur.execute(
                    """
                    SELECT id, venue_id, title, genre, starts_at, status, base_price
                    FROM events
                    WHERE genre = %s
                    ORDER BY id
                    LIMIT %s;
                    """,
                    (genre, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, venue_id, title, genre, starts_at, status, base_price
                    FROM events
                    ORDER BY id
                    LIMIT %s;
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "venue_id": row[1],
            "title": row[2],
            "genre": row[3],
            "starts_at": row[4].isoformat(),
            "status": row[5],
            "base_price": str(row[6]),
        }
        for row in rows
    ]


@app.post("/events", status_code=201)
def create_event(payload: EventCreate):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (venue_id, title, genre, starts_at, status, base_price)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, venue_id, title, genre, starts_at, status, base_price;
                """,
                (
                    payload.venue_id,
                    payload.title,
                    payload.genre,
                    payload.starts_at,
                    payload.status,
                    payload.base_price,
                ),
            )
            row = cur.fetchone()
        conn.commit()

    return {
        "id": row[0],
        "venue_id": row[1],
        "title": row[2],
        "genre": row[3],
        "starts_at": row[4].isoformat(),
        "status": row[5],
        "base_price": str(row[6]),
    }


@app.get("/customers")
def list_customers(
    loyalty_tier: Annotated[str | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if loyalty_tier:
                cur.execute(
                    """
                    SELECT id, full_name, email, loyalty_tier, created_at
                    FROM customers
                    WHERE loyalty_tier = %s
                    ORDER BY id
                    LIMIT %s;
                    """,
                    (loyalty_tier, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, full_name, email, loyalty_tier, created_at
                    FROM customers
                    ORDER BY id
                    LIMIT %s;
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "full_name": row[1],
            "email": row[2],
            "loyalty_tier": row[3],
            "created_at": row[4].isoformat(),
        }
        for row in rows
    ]


@app.post("/customers", status_code=201)
def create_customer(payload: CustomerCreate):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (full_name, email, loyalty_tier)
                VALUES (%s, %s, %s)
                RETURNING id, full_name, email, loyalty_tier, created_at;
                """,
                (payload.full_name, payload.email, payload.loyalty_tier),
            )
            row = cur.fetchone()
        conn.commit()

    return {
        "id": row[0],
        "full_name": row[1],
        "email": row[2],
        "loyalty_tier": row[3],
        "created_at": row[4].isoformat(),
    }