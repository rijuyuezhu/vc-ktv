"""Common HTTP error for application and router boundaries."""

from fastapi import HTTPException


def bail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)
