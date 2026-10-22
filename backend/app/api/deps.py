from __future__ import annotations

from fastapi import HTTPException, Request

from app.container import Container


def C(request: Request) -> Container:
    return request.app.state.container


def http_error(status: int, message: str, detail=None) -> HTTPException:
    body = {"message": message}
    if detail is not None:
        body["detail"] = detail
    return HTTPException(status_code=status, detail=body)
