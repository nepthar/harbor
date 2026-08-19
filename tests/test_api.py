"""The admin API surface: what it projects, what it refuses, and what it runs."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from harbor.daemon.api import API_VERSION, create_app
from harbor.daemon.jobs import JobRunner
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx

APP = "io.p2net.basic-features"


def ctx() -> HarborCtx:
  config = load_config()
  assert config is not None
  return HarborCtx(config)


@pytest.fixture
def jobs() -> JobRunner:
  """A runner with no worker thread; tests drain it with `run_pending`."""
  return JobRunner(ctx)


@pytest.fixture
def client(jobs: JobRunner) -> TestClient:
  return TestClient(create_app(ctx, jobs))


def submit(client: TestClient, jobs: JobRunner, verb: str, args: dict[str, str]):
  """Submit and run one job, returning its finished record."""
  response = client.post("/jobs", json={"verb": verb, "args": args})
  assert response.status_code == 202, response.text
  jobs.run_pending()
  return jobs.get(response.json()["id"])


def test_version(harbor_env, client):
  body = client.get("/version").json()
  assert body["api"] == API_VERSION
  assert body["harbor"]
  # The root is the same answer, so a bare curl at the socket says something.
  assert client.get("/").json() == body


def test_apps_lists_only_installed(harbor_env, client):
  # Every fixture is in the catalog; none is installed until it is staged.
  assert client.get("/apps").json()["apps"] == []

  harbor_env.run("stage", "basic-features")
  apps = client.get("/apps").json()["apps"]
  assert [app["app_id"] for app in apps] == [APP]

  app = apps[0]
  assert app["display_name"] == "Basic Features"
  assert app["status"] == "stopped"
  assert app["staged"] is True
  assert app["volume_count"] == 3
  assert app["containers"] == {"running": 0, "total": 0}
  # admin_user has no default and was never set, so the app cannot start yet.
  assert app["configured"] == "missing"


def test_apps_reflects_running_containers(harbor_env, client):
  harbor_env.run("start", "basic-features", "--set", "admin_user=root")

  app = client.get("/apps").json()["apps"][0]
  assert app["status"] == "running"
  assert app["containers"] == {"running": 1, "total": 1}
  assert app["configured"] == "ready"
  assert app["last_action"] == "started"


def test_app_detail(harbor_env, client):
  harbor_env.run("stage", "basic-features")
  response = client.get(f"/apps/{APP}")
  assert response.status_code == 200

  body = response.json()
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


def test_app_detail_never_projects_a_secret(harbor_env, client):
  harbor_env.run("start", "basic-features", "--set", "admin_user=root")

  body = client.get(f"/apps/{APP}").json()
  config = {c["name"]: c for c in body["config"]}
  assert config["admin_pass"]["secret"] is True
  assert config["admin_pass"]["set"] is True
  assert config["admin_pass"]["value"] is None

  # A non-secret value is shown, exactly as `harbor inspect` prints it.
  assert config["admin_user"]["secret"] is False
  assert config["admin_user"]["value"] == "root"

  # And the generated secret appears nowhere in the serialized response.
  _, secret = ctx().app_store(APP).get_config("admin_pass")
  assert secret
  assert secret not in json.dumps(body)


def test_unknown_app_is_404(harbor_env, client):
  assert client.get("/apps/nope").status_code == 404
  # Not a valid app id at all.
  assert client.get("/apps/not a name!").status_code == 404


def test_method_not_allowed(harbor_env, client):
  assert client.delete("/apps").status_code == 405
  assert client.put("/nope").status_code == 404


def test_stop_runs_as_a_job(harbor_env, client, jobs):
  harbor_env.run("start", "basic-features", "--set", "admin_user=root")
  assert client.get("/apps").json()["apps"][0]["status"] == "running"

  job = submit(client, jobs, "stop", {"app": "basic-features"})
  assert job["state"] == "done"
  assert job["error"] is None
  assert job["output"] == f"Stopped {APP}"
  assert job["started_at"] and job["finished_at"]

  assert client.get("/apps").json()["apps"][0]["status"] == "stopped"
  assert client.get(f"/jobs/{job['id']}").json() == job


def test_failed_job_carries_the_error(harbor_env, client, jobs):
  harbor_env.run("stage", "basic-features")

  job = submit(client, jobs, "start", {"app": "basic-features"})
  assert job["state"] == "failed"
  assert "admin_user is unset" in job["error"]
  assert job["output"] == ""


def test_jobs_are_listed_newest_first(harbor_env, client, jobs):
  harbor_env.run("stage", "basic-features")
  submit(client, jobs, "stage", {"app": "basic-features"})
  submit(client, jobs, "stop", {"app": "basic-features"})

  listed = client.get("/jobs").json()["jobs"]
  assert [job["verb"] for job in listed] == ["stop", "stage"]


def test_unknown_job_is_404(harbor_env, client):
  assert client.get("/jobs/deadbeef").status_code == 404


@pytest.mark.parametrize(
  ("body", "expected"),
  [
    ([1, 2], "JSON object"),
    ({"args": {"app": "basic-features"}}, '"verb" string'),
    ({"verb": "rm", "args": {"app": "basic-features"}}, "Unknown verb"),
    ({"verb": "stop", "args": {}}, "requires argument"),
    ({"verb": "stop", "args": {"app": 3}}, "string values"),
  ],
)
def test_submission_is_refused_with_a_reason(harbor_env, client, body, expected):
  response = client.post("/jobs", json=body)
  assert response.status_code == 400
  assert expected in response.json()["error"]


def test_empty_body_is_refused(harbor_env, client):
  response = client.post("/jobs")
  assert response.status_code == 400
  assert "not valid JSON" in response.json()["error"]


@pytest.mark.parametrize(
  "app",
  ["nope", "/etc/passwd", "../../etc", "tests/fixtures/apps/ports-demo.happ"],
)
def test_verbs_take_ids_of_installed_apps_and_nothing_else(harbor_env, client, app):
  """The rule that bounds the blast radius: no path ever reaches a verb.

  A path argument is how a caller defines what an app *is* -- which volumes it
  binds, which image it runs -- and that is root. `harbor stage <path>` stays
  a CLI-only capability.
  """
  response = client.post("/jobs", json={"verb": "stage", "args": {"app": app}})
  assert response.status_code == 400
  assert "No app found" in response.json()["error"]


def test_verbs_reject_arguments_they_do_not_declare(harbor_env, client):
  response = client.post(
    "/jobs",
    json={
      "verb": "stop",
      "args": {"app": "basic-features", "bundle": "/tmp/evil.happ"},
    },
  )
  assert response.status_code == 400
  assert "no argument 'bundle'" in response.json()["error"]
