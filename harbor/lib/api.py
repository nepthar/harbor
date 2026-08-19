"""The harbor admin API, as a function.

`dispatch` is deliberately transport-free: request in, response out, no
sockets. That keeps the whole API testable in-process at the speed of the rest
of the suite, and it means swapping the transport later -- for streaming, say
-- does not touch a single route.

Reads do not take the harbor lock. They walk the filesystem the same way
`harbor ps` does, and the worst a concurrent write can do is show a listing
that was true a moment ago. Writes go through `JobRunner`, which does lock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harbor import VERSION
from harbor.lib import views
from harbor.lib.apps import AppID
from harbor.lib.harbor import HarborCtx
from harbor.lib.jobs import JobRunner, validate

# Bumped when a response shape changes in a way a client would notice. The web
# UI ships separately from the daemon, so it has to be able to tell.
API_VERSION = 1


@dataclass(frozen=True)
class Response:
  status: int
  body: Any


def _error(status: int, message: str) -> Response:
  return Response(status, {"error": message})


def dispatch(
  method: str,
  path: str,
  body: dict[str, Any] | None,
  ctx_factory: Callable[[], HarborCtx],
  jobs: JobRunner,
) -> Response:
  """Route one request. `ctx_factory` is called at most once, per request, so
  a request sees harbor state as of when it arrived -- same as an invocation."""
  parts = tuple(p for p in path.split("?")[0].strip("/").split("/") if p)

  match (method, parts):
    case ("GET", ()) | ("GET", ("version",)):
      return Response(200, {"harbor": VERSION, "api": API_VERSION})

    case ("GET", ("apps",)):
      return Response(200, {"apps": views.apps_view(ctx_factory())})

    case ("GET", ("apps", app_id)):
      return _app(app_id, ctx_factory())

    case ("GET", ("jobs",)):
      return Response(200, {"jobs": jobs.list()})

    case ("POST", ("jobs",)):
      return _submit(body, ctx_factory(), jobs)

    case ("GET", ("jobs", job_id)):
      job = jobs.get(job_id)
      if job is None:
        return _error(404, f"No job {job_id!r}")
      return Response(200, job)

    case (_, ()) | (_, ("version",)) | (_, ("apps",)) | (_, ("jobs",)):
      return _error(405, f"{method} not allowed here")

    case _:
      return _error(404, f"No route for {path}")


def _app(app_id: str, ctx: HarborCtx) -> Response:
  try:
    resolved = AppID(app_id)
  except ValueError:
    return _error(404, f"No app {app_id!r}")
  try:
    return Response(200, views.app_view(resolved, ctx))
  except (ValueError, RuntimeError) as e:
    return _error(404, str(e))


def _submit(body: dict[str, Any] | None, ctx: HarborCtx, jobs: JobRunner) -> Response:
  if not isinstance(body, dict):
    return _error(400, 'Body must be a JSON object: {"verb": ..., "args": {...}}')

  verb = body.get("verb")
  args = body.get("args", {})
  if not isinstance(verb, str):
    return _error(400, 'Body needs a "verb" string')
  if not isinstance(args, dict) or not all(
    isinstance(k, str) and isinstance(v, str) for k, v in args.items()
  ):
    return _error(400, '"args" must be an object of string values')

  try:
    validate(verb, args, ctx)
  except (ValueError, RuntimeError) as e:
    return _error(400, str(e))

  return Response(202, jobs.submit(verb, args))
