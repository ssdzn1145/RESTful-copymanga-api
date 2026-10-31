import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "cloud-functions"))

from backend import app

_wsgi_app = app.wsgi_app


def _restore_path(environ, start_response):
    query = environ.get("QUERY_STRING", "")
    values = parse_qs(query).get("__p")
    if values:
        environ["PATH_INFO"] = values[0]
        environ["QUERY_STRING"] = "&".join(
            part for part in query.split("&") if part and not part.startswith("__p=")
        )
    return _wsgi_app(environ, start_response)


app.wsgi_app = _restore_path
