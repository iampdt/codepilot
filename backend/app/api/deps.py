"""Request dependencies: the runtime, a database session, and the optional access token."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def require_token(request: Request) -> None:
    """When API_TOKEN is set, every route needs it. EventSource can't send headers, so the
    stream also accepts it as a query parameter."""
    expected = request.app.state.runtime.settings.api_token
    if not expected:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    supplied = supplied or request.query_params.get("token", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong access token")


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
