"""The admin API surface: what it projects, what it refuses, and what it runs.

Everything here goes through `dispatch`, not a socket. The daemon owns only
the transport, and a test that binds one would buy coverage of `http.server`.
"""

from __future__ import annotations

import json

import pytest

from harbor.lib.api import API_VERSION, dispatch
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx
from harbor.lib.jobs import JobRunner

APP = "io.p2net.basic-features"


def ctx() -> HarborCtx:
  config = load_config()
  assert config is not None
  return HarborCtx(config)


@pytest.fixture
def jobs() -> JobRunner:
  """A runner with no worker thread; tests drain it with `run_pending`."""
  return JobRunner(ctx)


def get(path: str, jobs: JobRunner):
  return dispatch("GET", path, None, ctx, jobs)


def post(path: str, body, jobs: JobRunner):
  return dispatch("POST", path, body, ctx, jobs)


def submit(verb: str, args: dict[str, str], jobs: JobRunner):
  """Submit and run one job, returning its finished record."""
  response = post("/jobs", {"verb": verb, "args": args}, jobs)
  assert response.status == 202, response.body
  jobs.run_pending()
  return jobs.get(response.body["id"])


def test_version(harbor_env, jobs):
  response = get("/version", jobs)
  assert response.status == 200
  assert response.body["api"] == API_VERSION
  assert response.body["harbor"]


def test_apps_lists_only_installed(harbor_env, jobs):
  # Every fixture is in the catalog; none is installed until it is staged.
  assert get("/apps", jobs).body["apps"] == []

  harbor_env.run("stage", "basic-features")
  apps = get("/apps", jobs).body["apps"]
  assert [app["app_id"] for app in apps] == [APP]

  app = apps[0]
  assert app["display_name"] == "Basic Features"
  assert app["status"] == "stopped"
  assert app["staged"] is True
  assert app["volume_count"] == 3
  assert app["containers"] == {"running": 0, "total": 0}
  # admin_user has no default and was never set, so the app cannot start yet.
  assert app["configured"] == "missing"


def test_apps_reflects_running_containers(harbor_env, jobs):
  harbor_env.run("start", "basic-features", "--set", "admin_user=root")

  app = get("/apps", jobs).body["apps"][0]
  assert app["status"] == "running"
  assert app["containers"] == {"running": 1, "total": 1}
  assert app["configured"] == "ready"
  assert app["last_action"] == "started"


def test_app_detail(harbor_env, jobs):
  harbor_env.run("stage", "basic-features")
  response = get(f"/apps/{APP}", jobs)
  assert response.status == 200

  body = response.body
  assert body["description"] == "Config and volumes fixture"
  assert [unit["name"] for unit in body["units"]] == ["main"]
  assert body["units"][0]["image"] == "alpine:latest"
  assert body["units"][0]["state"] is None

  volumes = {v["name"]: v for v in body["volumes"]}
  assert set(volumes) == {"config", "cache", "bin"}
  assert volumes["config"]["kind"] == "data"
  assert volumes["bin"]["readonly"] is True
  assert body["volume_bytes"] >= 0

  # admin_user is the one thing standing between this app and a start.
  assert [issue["problem"] for issue in body["issues"]] == [
    "config admin_user is unset and no default specified"
  ]


def test_app_detail_never_projects_a_secret(harbor_env, jobs):
  harbor_env.run("start", "basic-features", "--set", "admin_user=root")

  config = {c["name"]: c for c in get(f"/apps/{APP}", jobs).body["config"]}
  assert config["admin_pass"]["secret"] is True
  assert config["admin_pass"]["set"] is True
  assert config["admin_pass"]["value"] is None

  # A non-secret value is shown, exactly as `harbor inspect` prints it.
  assert config["admin_user"]["secret"] is False
  assert config["admin_user"]["value"] == "root"

  # And the generated secret appears nowhere in the serialized response.
  _, secret = ctx().app_store(APP).get_config("admin_pass")
  assert secret
  assert secret not in json.dumps(get(f"/apps/{APP}", jobs).body)


def test_unknown_app_is_404(harbor_env, jobs):
  assert get("/apps/nope", jobs).status == 404
  # Not a valid app id at all.
  assert get("/apps/..%2Fetc", jobs).status == 404


def test_method_not_allowed(harbor_env, jobs):
  assert dispatch("DELETE", "/apps", None, ctx, jobs).status == 405
  assert dispatch("PUT", "/nope", None, ctx, jobs).status == 404


def test_stop_runs_as_a_job(harbor_env, jobs):
  harbor_env.run("start", "basic-features", "--set", "admin_user=root")
  assert get("/apps", jobs).body["apps"][0]["status"] == "running"

  job = submit("stop", {"app": "basic-features"}, jobs)
  assert job["state"] == "done"
  assert job["error"] is None
  assert job["output"] == f"Stopped {APP}"
  assert job["started_at"] and job["finished_at"]

  assert get("/apps", jobs).body["apps"][0]["status"] == "stopped"


def test_failed_job_carries_the_error(harbor_env, jobs):
  harbor_env.run("stage", "basic-features")

  job = submit("start", {"app": "basic-features"}, jobs)
  assert job["state"] == "failed"
  assert "admin_user is unset" in job["error"]
  assert job["output"] == ""


def test_jobs_are_listed_newest_first(harbor_env, jobs):
  harbor_env.run("stage", "basic-features")
  submit("stage", {"app": "basic-features"}, jobs)
  submit("stop", {"app": "basic-features"}, jobs)

  listed = jobs.list()
  assert [job["verb"] for job in listed] == ["stop", "stage"]
  assert get("/jobs", jobs).body["jobs"] == listed


def test_unknown_job_is_404(harbor_env, jobs):
  assert get("/jobs/deadbeef", jobs).status == 404


@pytest.mark.parametrize(
  ("body", "expected"),
  [
    (None, "JSON object"),
    ({"args": {"app": "basic-features"}}, '"verb" string'),
    ({"verb": "rm", "args": {"app": "basic-features"}}, "Unknown verb"),
    ({"verb": "stop", "args": {}}, "requires argument"),
    ({"verb": "stop", "args": {"app": 3}}, "string values"),
  ],
)
def test_submission_is_refused_with_a_reason(harbor_env, jobs, body, expected):
  response = post("/jobs", body, jobs)
  assert response.status == 400
  assert expected in response.body["error"]


@pytest.mark.parametrize(
  "app",
  ["nope", "/etc/passwd", "../../etc", "tests/fixtures/apps/ports-demo.happ"],
)
def test_verbs_take_ids_of_installed_apps_and_nothing_else(harbor_env, jobs, app):
  """The rule that bounds the blast radius: no path ever reaches a verb.

  A path argument is how a caller defines what an app *is* -- which volumes it
  binds, which image it runs -- and that is root. `harbor stage <path>` stays
  a CLI-only capability.
  """
  response = post("/jobs", {"verb": "stage", "args": {"app": app}}, jobs)
  assert response.status == 400
  assert "No app found" in response.body["error"]


def test_verbs_reject_arguments_they_do_not_declare(harbor_env, jobs):
  response = post(
    "/jobs",
    {"verb": "stop", "args": {"app": "basic-features", "bundle": "/tmp/evil.happ"}},
    jobs,
  )
  assert response.status == 400
  assert "no argument 'bundle'" in response.body["error"]
