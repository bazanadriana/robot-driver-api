from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

# We import the richer "run" function from robot.py
# NOTE: adjust this import if your folder name isn't exactly "src".
from src.robot import run as run_robot


app = FastAPI(
    title="Robot Driver API",
    description=(
        "API to scrape Amazon for the first matching product. "
        "It can log in (if creds are provided), open the first search result, "
        "and return title / price / product URL."
    ),
    version="1.1.0",
)


# ----------- Request / Response Models -----------

class RunRequest(BaseModel):
    query: str  # e.g. "Apple AirPods Pro"


class RunResponse(BaseModel):
    status: str                  # "SUCCESS" or "FAILURE"
    query: str
    title: Optional[str] = None  # product title (if success)
    price: Optional[str] = None  # product price (if success)
    url: Optional[str] = None    # product URL (if success)
    reason: Optional[str] = None # error message (if failure)


# ----------- Endpoint -----------

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
            stay_open=False,              # don't block the server waiting for ENTER
            interactive_login=False       # don't pause for CAPTCHA/MFA in API mode
        )

    except Exception as e:
        # If something goes really wrong at runtime:
        raise HTTPException(
            status_code=500,
            detail={
                "status": "FAILURE",
                "query": req.query,
                "reason": f"SERVER_CRASH: {e.__class__.__name__}: {e}",
            },
        )

    # If we got a price, treat it as success.
    if price:
        return RunResponse(
            status="SUCCESS",
            query=req.query,
            title=title,
            price=price,
            url=url_or_err,  # in success case, url_or_err is actually the product URL
            reason=None,
        )

    # Otherwise it's a failure, and url_or_err contains the error string
    return RunResponse(
        status="FAILURE",
        query=req.query,
        title=None,
        price=None,
        url=None,
        reason=url_or_err or "Price not found",
    )
