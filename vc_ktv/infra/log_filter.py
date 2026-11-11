"""Remove credentials from URL-bearing server access logs."""

import logging
import re


class RedactURLSecrets(logging.Filter):
    """Keep legacy TV bearer and short-lived pairing links out of access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            redacted = re.sub(
                r"(?i)([?&](?:token|pair)=)[^&\s]+", r"\1[redacted]", path
            )
            if path != redacted:
                record.args = (*args[:2], redacted, *args[3:])
        return True
