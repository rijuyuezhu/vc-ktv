"""Common HTTP error for application and router boundaries."""

from typing import NoReturn

from fastapi import HTTPException


def bail(status: int, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail=message)
