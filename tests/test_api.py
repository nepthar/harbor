"""The admin API surface: what it projects, what it refuses, and what it runs."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

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
    # Shape, rejected by the JobSubmission model...
    ([1, 2], "valid dictionary"),
    ({"args": {"app": "basic-features"}}, "verb: Field required"),
    ({"verb": "stop", "args": {"app": 3}}, "args.app: Input should be a valid str"),
    ({"verb": "stop", "app": "basic-features"}, "app: Extra inputs"),
    # ...and meaning, which only a live context can judge.
    ({"verb": "rm", "args": {"app": "basic-features"}}, "Unknown verb"),
    ({"verb": "stop", "args": {}}, "requires argument"),
  ],
)
def test_submission_is_refused_with_a_reason(harbor_env, client, body, expected):
  response = client.post("/jobs", json=body)
  assert response.status_code == 400, response.text
  assert expected in response.json()["error"]


def test_empty_body_is_refused(harbor_env, client):
  response = client.post("/jobs")
  assert response.status_code == 400
  assert "error" in response.json()


def test_every_error_has_the_same_shape(harbor_env, client):
  """One key to read, whether harbor refused, the route is missing, or the
  body was malformed. Two shapes means a client gets one of them wrong."""
  for response in (
    client.get("/apps/nope"),
    client.get("/nowhere"),
    client.delete("/apps"),
    client.post("/jobs", json={"nonsense": True}),
  ):
    assert response.status_code >= 400
    assert set(response.json()) == {"error"}, response.text


def test_openapi_documents_the_surface(harbor_env, client):
  """Free with FastAPI, and the web UI is a separate codebase that has to
  discover this API somehow."""
  spec = client.get("/openapi.json")
  assert spec.status_code == 200
  paths = spec.json()["paths"]
  assert {"/apps", "/apps/{app_id}", "/jobs", "/jobs/{job_id}"} <= set(paths)
  assert "post" in paths["/jobs"]


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


# --- host volumes ----------------------------------------------------------


def test_host_volumes_round_trip_over_the_api(harbor_env, client):
  fresh = harbor_env.root / "extra-data"
  fresh.mkdir()

  listed = client.get("/host-volumes").json()["host_volumes"]
  assert [v["tag"] for v in listed] == ["media", "other"]

  created = client.post(
    "/host-volumes", json={"tag": "extra", "path": str(fresh), "readonly": True}
  )
  assert created.status_code == 201, created.text
  entry = {v["tag"]: v for v in created.json()["host_volumes"]}["extra"]
  assert entry["path"] == str(fresh)
  assert entry["readonly"] is True
  assert entry["exists"] is True

  # The response reflects config.toml after the write, not the request's own
  # stale read of it.
  assert "extra" in harbor_env.config.read_text()

  replaced = client.put("/host-volumes/extra", json={"path": str(fresh)})
  assert replaced.status_code == 200
  assert {v["tag"]: v for v in replaced.json()["host_volumes"]}["extra"][
    "readonly"
  ] is False

  removed = client.delete("/host-volumes/extra")
  assert removed.status_code == 200
  assert "extra" not in [v["tag"] for v in removed.json()["host_volumes"]]
  assert "[host_volume.extra]" not in harbor_env.config.read_text()


def test_host_volume_refusals_keep_config_intact(harbor_env, client):
  before = harbor_env.config.read_text()

  missing = client.post("/host-volumes", json={"tag": "x", "path": "/no/such/dir"})
  assert missing.status_code == 400
  assert "No such directory" in missing.json()["error"]

  duplicate = client.post(
    "/host-volumes", json={"tag": "media", "path": str(harbor_env.root)}
  )
  assert duplicate.status_code == 400
  assert "already exists" in duplicate.json()["error"]

  assert client.put("/host-volumes/nope", json={"path": "/tmp"}).status_code == 404
  assert client.delete("/host-volumes/nope").status_code == 404

  assert harbor_env.config.read_text() == before


def test_volumes_view_reports_ownership_and_use(harbor_env, client):
  assert harbor_env.run("start", APP, "--set", "admin_user=root").returncode == 0
  (harbor_env.volumes_root / "data" / APP / "config" / "db.txt").write_text("xy")

  volumes = {v["name"]: v for v in client.get("/volumes").json()["volumes"]}
  assert volumes["config"]["app_id"] == APP
  assert volumes["config"]["kind"] == "data"
  assert volumes["config"]["in_use"] is True
  assert volumes["config"]["declared"] is True
  # Sizes are opt-in, because measuring walks every file under every volume.
  assert volumes["config"]["bytes"] is None

  sized = {v["name"]: v for v in client.get("/volumes?sizes=1").json()["volumes"]}
  assert sized["config"]["bytes"] == 2


def test_volume_data_outliving_its_manifest_still_shows_up(harbor_env, client):
  """Re-staging drops the link of a volume the manifest stopped declaring and
  leaves the data. Nothing else would ever tell you it is still on disk."""
  assert harbor_env.run("start", APP, "--set", "admin_user=root").returncode == 0
  assert harbor_env.run("stop", APP).returncode == 0
  assert {v["name"] for v in client.get("/volumes").json()["volumes"]} == {
    "config",
    "cache",
  }

  manifest = harbor_env.root / "apps" / f"{APP}.happ" / "manifest.toml"
  # Dropped from [volumes] *and* from the unit that mounted it -- a manifest
  # that declares neither is what re-staging leaves data behind for.
  text = manifest.read_text().replace('config = { kind = "data" }', "")
  manifest.write_text(text.replace('config = "/myapp/config", ', ""))
  assert harbor_env.run("stage", APP).returncode == 0

  volumes = {v["name"]: v for v in client.get("/volumes").json()["volumes"]}
  assert volumes["config"]["declared"] is False, "data left behind, flagged"
  assert volumes["cache"]["declared"] is True
