import logging
import re
import time
import uuid

logger = logging.getLogger("request")

# Only reuse a client's X-Request-ID if it looks safe (stops junk / fake log lines)
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class RequestLoggingMiddleware:
    """
    For every request:
      - X-Request-ID: reuse the incoming one if valid, otherwise generate one
      - X-Response-Time: how long the request took
      - one log line: method path status user:role duration request_id
    """

    def __init__(self, get_response):
        # Runs once, at server start
        self.get_response = get_response

    def __call__(self, request):
        # BEFORE the view
        incoming_id = request.headers.get("X-Request-ID", "")
        request_id = incoming_id if VALID_REQUEST_ID.match(incoming_id) else uuid.uuid4().hex
        request.request_id = request_id  # so views can use it later (e.g. pass to Celery)
        start = time.perf_counter()

        response = self.get_response(request)

        # AFTER the view: DRF has checked the JWT by now, so request.user is the real user
        duration_ms = (time.perf_counter() - start) * 1000
        response["X-Request-ID"] = request_id
        response["X-Response-Time"] = f"{duration_ms:.2f}ms"

        logger.info(
            "%s %s %s %s %.2fms %s",
            request.method,
            request.get_full_path(),
            response.status_code,
            self.user_label(request),
            duration_ms,
            request_id,
        )
        return response

    def user_label(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return "anonymous"
        if user.is_superuser:
            return f"{user.email}:admin"
        group = user.groups.first()
        return f"{user.email}:{group.name if group else 'none'}"
