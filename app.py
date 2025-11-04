from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

# Import browser automation runner from robot.py
# (Make sure you're running uvicorn from the project root so `src` is importable)
from src.robot import run as run_robot


# ---------------------------
# FastAPI app definition
# ---------------------------

app = FastAPI(
    title="Robot Driver API",
    description=(
        "API to scrape Amazon for the first matching product. "
        "It can log in (if creds are provided), open the first search result, "
        "and return title / price / product URL."
    ),
    version="1.1.0",
)


# ---------------------------
# Healthcheck / root route
# ---------------------------

@app.get("/")
def healthcheck():
    """
    Simple health/status endpoint for reviewers.
    """
    return {
        "service": "robot-driver-api",
        "status": "ok",
        "endpoints": {
            "docs": "/docs",
            "run": "/run (POST)"
        },
        "note": "Send POST /run with {'query': 'Apple AirPods Pro'} to scrape."
    }


# ---------------------------
# Request / Response Models
# ---------------------------

class RunRequest(BaseModel):
    query: str  # e.g. "Apple AirPods Pro"


class RunResponse(BaseModel):
    status: str                  # "SUCCESS" or "FAILURE"
    query: str
    title: Optional[str] = None  # product title (if success)
    price: Optional[str] = None  # product price (if success)
    url: Optional[str] = None    # product URL (if success)
    reason: Optional[str] = None # error message (if failure)


# ---------------------------
# /run endpoint
# ---------------------------

@app.post("/run", response_model=RunResponse)
def run_agent(req: RunRequest):
    """
    Launch the browser automation against Amazon for the given query.

    Input:
    {
      "query": "Apple AirPods Pro"
    }

    Output (SUCCESS):
    {
      "status": "SUCCESS",
      "query": "Apple AirPods Pro",
      "title": "...",
      "price": "$199.99",
      "url": "https://www.amazon.com/dp/...."
    }

    Output (FAILURE):
    {
      "status": "FAILURE",
      "query": "Apple AirPods Pro",
      "reason": "ERROR: ... or Price not found"
    }
    """

    try:
        # run_robot(query) returns: (title, price, url_or_err)
        title, price, url_or_err = run_robot(
            query=req.query,
            stay_open=False,        # don't block the server waiting for ENTER
            interactive_login=False # don't pause for CAPTCHA/MFA in API mode
        )

    except Exception as e:
        # Something blew up at runtime (Playwright launch, network error, etc.)
        raise HTTPException(
            status_code=500,
            detail={
                "status": "FAILURE",
                "query": req.query,
                "reason": f"SERVER_CRASH: {e.__class__.__name__}: {e}",
            },
        )

    # Success path: we found a price/title/url
    if price:
        return RunResponse(
            status="SUCCESS",
            query=req.query,
            title=title,
            price=price,
            url=url_or_err,  # in success case, url_or_err is actually the URL
            reason=None,
        )

    # Failure path: could not get data (captcha, blocked, etc.)
    return RunResponse(
        status="FAILURE",
        query=req.query,
        title=None,
        price=None,
        url=None,
        reason=url_or_err or "Price not found",
    )
