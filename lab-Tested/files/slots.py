import re
import secrets

from flask import redirect, request

from files.ansible import app

SLOT_RE = re.compile(r"^/u/([A-Za-z0-9]{1,16})(/.*)?$")
COOKIE = "session"
ABSOLUTE_RE = re.compile(r"^(https?://[^/]+)?(/.*)$")
ATTR_RE = re.compile(r'(\b(?:href|action|src)=)(["\'])/(?!/)([^"\']*)', re.I)


class SlotMiddleware:
    """Serves each account under /u/<slot>/ with its own session cookie."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    @staticmethod
    def _cookies_in(header, slot):
        wanted = f"{COOKIE}_{slot}"
        kept = []

        for part in header.split(";"):
            part = part.strip()

            if not part:
                continue

            name, _, value = part.partition("=")

            if name == COOKIE:
                continue

            if name == wanted:
                kept.append(f"{COOKIE}={value}")
            else:
                kept.append(part)

        return "; ".join(kept)

    @staticmethod
    def _fix_location(value, prefix, host):
        if value.startswith("//"):
            return value

        match = ABSOLUTE_RE.match(value)

        if not match:
            return value

        origin, path = match.group(1) or "", match.group(2)

        if origin and origin.split("://", 1)[1] != host:
            return value

        if path.startswith("/u/") or path.startswith("/static/"):
            return value

        return origin + prefix + path

    def __call__(self, environ, start_response):
        match = SLOT_RE.match(environ.get("PATH_INFO", ""))

        if not match:
            return self.wsgi_app(environ, start_response)

        slot = match.group(1)
        prefix = "/u/" + slot
        host = environ.get("HTTP_HOST", "")

        environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + prefix
        environ["PATH_INFO"] = match.group(2) or "/"
        environ["labs.prefix"] = prefix
        environ["HTTP_COOKIE"] = self._cookies_in(
            environ.get("HTTP_COOKIE", ""), slot
        )

        def _start(status, headers, exc_info=None):
            fixed = []

            for name, value in headers:
                lowered = name.lower()

                if lowered == "set-cookie" and value.startswith(COOKIE + "="):
                    value = f"{COOKIE}_{slot}=" + value[len(COOKIE) + 1:]
                elif lowered == "location":
                    value = self._fix_location(value, prefix, host)

                fixed.append((name, value))

            return start_response(status, fixed, exc_info)

        return self.wsgi_app(environ, _start)


app.wsgi_app = SlotMiddleware(app.wsgi_app)


@app.after_request
def prefix_html_links(resp):
    prefix = request.environ.get("labs.prefix")

    if (
        not prefix
        or resp.mimetype != "text/html"
        or resp.direct_passthrough
    ):
        return resp

    def fix(match):
        rest = match.group(3)

        if rest.startswith(("u/", "static/")):
            return match.group(0)

        return f"{match.group(1)}{match.group(2)}{prefix}/{rest}"

    resp.set_data(ATTR_RE.sub(fix, resp.get_data(as_text=True)))
    return resp


@app.route("/new-account")
def new_account():
    return redirect("/u/" + secrets.token_hex(3) + "/login")