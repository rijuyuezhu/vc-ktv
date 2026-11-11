"""Run the VC KTV server with ``python -m vc_ktv``."""

import logging
import os

import uvicorn

from vc_ktv.application.factory import create_app
from vc_ktv.infra.log_filter import RedactURLSecrets

logging.getLogger("uvicorn.access").addFilter(RedactURLSecrets())

uvicorn.run(
    create_app(),
    host=os.getenv("VC_KTV_HOST", "127.0.0.1"),
    port=int(os.getenv("VC_KTV_PORT", "8778")),
)
