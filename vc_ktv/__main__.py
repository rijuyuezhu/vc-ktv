import uvicorn

from .application.factory import create_app

app = create_app()
if __name__ == "__main__":
    import os

    uvicorn.run(
        app,
        host=os.getenv("VC_KTV_HOST", "127.0.0.1"),
        port=int(os.getenv("VC_KTV_PORT", "8778")),
    )
