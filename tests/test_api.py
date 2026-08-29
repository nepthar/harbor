"""The admin API surface: what it projects, what it refuses, and what it runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harbor.daemon.api import API_VERSION, create_app
from harbor.jobs import JobRunner
from harbor.lib.config import load_config
from harbor.lib.harbor import HarborCtx
from harbor.lib.metric import record_volume_sizes

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


def read_log(job) -> str:
  """A finished job's output lives only in the file `log` names."""
  assert job["log"]
  config = load_config()
  assert config is not None
  return (config.activity_root / job["log"]).read_text()


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


def test_catalog_lists_available_apps_grouped_by_source(harbor_env, client):
  catalogs = client.get("/catalog").json()["catalogs"]
  assert [c["name"] for c in catalogs] == ["main"]

  apps = {app["app_id"]: app for app in catalogs[0]["apps"]}
  assert apps[APP]["display_name"] == "Basic Features"
  assert apps[APP]["version"] == "0.1.0"
  assert apps[APP]["description"] == "Config and volumes fixture"
  assert apps[APP]["repo"] == "main"
  assert apps[APP]["configured"] == "missing"
  assert 'display_name = "Basic Features"' in apps[APP]["manifest"]
  assert "[config]" in apps[APP]["manifest"]
  assert "ports-demo" in apps
  assert "routes-demo" in apps
  assert "host-volumes" in apps
  assert apps["ports-demo"]["configured"] == "ready"
  assert "routes = { web" in apps["ports-demo"]["manifest"]
  assert apps["routes-demo"]["configured"] == "ready"


def test_catalog_reports_installed_and_manifest_drift(harbor_env, client, jobs):
  """Installed-ness is the logtab; drift is the staged manifest vs the bundle's."""

  def entry():
    catalogs = client.get("/catalog").json()["catalogs"]
    return {app["app_id"]: app for app in catalogs[0]["apps"]}[APP]

  # A catalog entry alone is not an installation, and nothing is staged to
  # have drifted from.
  assert entry()["state"] == "available"
  assert entry()["manifest_stale"] is False

  assert submit(client, jobs, "install", {"app": APP})["state"] == "done"
  assert entry()["state"] == "installed"
  assert entry()["manifest_stale"] is False

  # Editing the bundle leaves the staged copy behind: that is the drift the
  # catalog card offers to close with a re-install.
  manifest = harbor_env.main_repo / f"{APP}.happ" / "manifest.toml"
  manifest.write_text(
    manifest.read_text() + "\n# a comment the staged copy has not seen\n"
  )
  assert entry()["manifest_stale"] is True

  assert submit(client, jobs, "install", {"app": APP})["state"] == "done"
  assert entry()["manifest_stale"] is False


def test_catalog_groups_a_second_repo(harbor_env, client):
  extra = harbor_env.root / "dev-apps"
  extra.mkdir()
  bundle = extra / "dev-app.happ"
  bundle.mkdir()
  (bundle / "manifest.toml").write_text(
    '[app]\nversion = "2.0"\ndisplay_name = "Dev App"\n'
    'description = "From a second catalog"\n'
    '[run.main]\nimage = "alpine:latest"\n'
  )
  with open(harbor_env.config, "a") as f:
    f.write(f'\n[repo.dev]\npath = "{extra}"\n')

  catalogs = {c["name"]: c["apps"] for c in client.get("/catalog").json()["catalogs"]}
  assert list(catalogs) == ["main", "dev"]
  assert APP in {app["app_id"] for app in catalogs["main"]}
  [dev] = catalogs["dev"]
  assert dev["app_id"] == "dev-app"
  assert dev["display_name"] == "Dev App"
  assert dev["version"] == "2.0"
  assert dev["description"] == "From a second catalog"
  assert dev["repo"] == "dev"
  assert dev["configured"] == "ready"
  assert 'display_name = "Dev App"' in dev["manifest"]


def test_catalog_keeps_a_broken_bundle(harbor_env, client):
  broken = harbor_env.main_repo / "broken.happ"
  broken.mkdir()
  (broken / "manifest.toml").write_text("not toml")

  apps = {
    app["app_id"]: app
    for catalog in client.get("/catalog").json()["catalogs"]
    for app in catalog["apps"]
  }
  assert apps["broken"] == {
    "app_id": "broken",
    "display_name": "",
    "version": None,
    "description": "",
    "repo": "main",
    "state": "available",
    "configured": None,
    "manifest": "not toml",
    "manifest_stale": False,
  }


def test_catalog_listing_does_not_create_config_stores(harbor_env, client):
  """Opening AppStore writes a logtab; a GET must not invent install state."""
  client.get("/catalog")
  assert not (harbor_env.root / "config" / f"{APP}.logtab").exists()


def test_catalog_config_turns_ready_once_required_values_are_set(harbor_env, client):
  assert (
    harbor_env.run("config", "basic-features", "--set", "admin_user=root").returncode
    == 0
  )
  apps = {
    app["app_id"]: app for app in client.get("/catalog").json()["catalogs"][0]["apps"]
  }
  assert apps[APP]["configured"] == "ready"


def test_apps_lists_only_installed(harbor_env, client):
  # Every fixture is in the catalog; none is installed until it is staged.
  assert client.get("/apps").json()["apps"] == []

  harbor_env.run("install", "basic-features")
  apps = client.get("/apps").json()["apps"]
  assert [app["app_id"] for app in apps] == [APP]

  app = apps[0]
  assert app["display_name"] == "Basic Features"
  assert app["status"] == "stopped"
  assert app["state"] == "installed"
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
  harbor_env.run("install", "basic-features")
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

  mounts = {v["name"]: v for v in body["units"][0]["volumes"]}
  assert mounts["config"]["path"] == "/myapp/config"
  assert mounts["config"]["desc"] == "persistent app data"
  assert mounts["cache"]["kind"] == "temp"
  assert mounts["cache"]["desc"] == ""
  assert mounts["bin"]["readonly"] is True
  assert mounts["bin"]["desc"] == "shipped binaries"

  config = {c["name"]: c for c in body["config"]}
  assert config["admin_user"]["advanced"] is False
  assert config["log_level"]["advanced"] is True
  assert config["log_level"]["value"] == "info"

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
  assert "output" not in job
  assert f"Stopped {APP}" in read_log(job)
  assert job["started_at"] and job["finished_at"]

  assert client.get("/apps").json()["apps"][0]["status"] == "stopped"
  assert client.get(f"/jobs/{job['id']}").json() == job


def _compose_calls(harbor_env) -> list[list[str]]:
  return [
    json.loads(line)["args"]
    for line in harbor_env.docker_log.read_text().splitlines()
    if json.loads(line)["args"][:1] == ["compose"]
  ]


def test_restart_stops_restages_and_starts_a_running_app(harbor_env, client, jobs):
  harbor_env.run("start", APP, "--set", "admin_user=root")
  manifest = harbor_env.main_repo / f"{APP}.happ" / "manifest.toml"
  manifest.write_text(manifest.read_text().replace("0.1.0", "0.2.0"))

  job = submit(client, jobs, "restart", {"app": APP})
  assert job["state"] == "done", job["error"]
  assert f"Restarted {APP}" in read_log(job)
  assert client.get(f"/apps/{APP}").json()["status"] == "running"
  staged = (harbor_env.run_root / APP / "happ" / "manifest.toml").read_text()
  assert 'version      = "0.2.0"' in staged
  assert _compose_calls(harbor_env) == [
    ["compose", "up", "-d"],
    ["compose", "down"],
    ["compose", "up", "-d"],
  ]


def test_restart_restages_a_stopped_app_without_starting(harbor_env, client, jobs):
  harbor_env.run("install", APP)
  manifest = harbor_env.main_repo / f"{APP}.happ" / "manifest.toml"
  manifest.write_text(manifest.read_text().replace("0.1.0", "0.2.0"))

  job = submit(client, jobs, "restart", {"app": APP})
  assert job["state"] == "done", job["error"]
  assert f"Restaged {APP}" in read_log(job)
  assert client.get("/apps").json()["apps"][0]["status"] == "stopped"
  staged = (harbor_env.run_root / APP / "happ" / "manifest.toml").read_text()
  assert 'version      = "0.2.0"' in staged
  assert _compose_calls(harbor_env) == []


def test_restart_unknown_app_is_refused(harbor_env, client):
  response = client.post("/jobs", json={"verb": "restart", "args": {"app": "nope"}})
  assert response.status_code == 400
  assert "No app found" in response.json()["error"]


def test_failed_job_carries_the_error(harbor_env, client, jobs):
  harbor_env.run("install", "basic-features")

  job = submit(client, jobs, "start", {"app": "basic-features"})
  assert job["state"] == "failed"
  assert "admin_user is unset" in job["error"]
  assert "admin_user is unset" in read_log(job)


def test_jobs_are_listed_newest_first(harbor_env, client, jobs):
  harbor_env.run("install", "basic-features")
  submit(client, jobs, "install", {"app": "basic-features"})
  submit(client, jobs, "stop", {"app": "basic-features"})

  listed = client.get("/jobs").json()["jobs"]
  assert [job["verb"] for job in listed] == ["stop", "install"]


def test_unknown_job_is_404(harbor_env, client):
  assert client.get("/jobs/deadbeef").status_code == 404


def test_a_job_files_activity_that_the_api_serves(harbor_env, client, jobs):
  job = submit(client, jobs, "install", {"app": "basic-features"})
  assert job["state"] == "done"
  # The finished job points at its own output file...
  assert job["log"] and job["log"].endswith(f".{APP}.install.log")

  runs = client.get("/activity").json()["activity"]
  assert runs[0]["verb"] == "install"
  assert runs[0]["app_id"] == APP
  assert runs[0]["status"] == "ok"
  assert runs[0]["log"] == job["log"]

  body = client.get(f"/activity/{job['log']}").json()
  assert body["app_id"] == APP
  assert f"Installed {APP}" in body["text"]


def test_a_failed_job_still_files_activity_with_its_error(harbor_env, client, jobs):
  harbor_env.run("install", "basic-features")
  job = submit(client, jobs, "start", {"app": "basic-features"})
  assert job["state"] == "failed"

  runs = client.get("/activity").json()["activity"]
  assert runs[0]["status"] == "error"
  body = client.get(f"/activity/{job['log']}").json()
  assert "admin_user is unset" in body["text"]


def test_a_running_job_tees_output_to_its_log(harbor_env, jobs, monkeypatch):
  """The UI polls `job.log` while a command runs; the file must already exist
  and already contain what has been printed."""
  import logging

  from harbor.jobs import JOBS, Job

  class LiveJob(Job):
    name = "install"
    required_args = ("app",)

    def init(self, ctx, kwargs):
      self.app = str(ctx.resolve_app(kwargs["app"]))

    def run(self, ctx) -> None:
      logging.getLogger("harbor").info("live line")
      running = [job for job in jobs.list() if job["state"] == "running"]
      assert len(running) == 1
      assert running[0]["log"]
      text = (ctx.config.activity_root / running[0]["log"]).read_text()
      assert "# harbor install" in text
      assert "live line" in text
      logging.getLogger("harbor").info("done")

  monkeypatch.setitem(JOBS, "install", LiveJob)
  job = jobs.submit("install", {"app": "basic-features"}, ctx())
  jobs.run_pending()
  finished = jobs.get(job["id"])
  assert finished is not None
  assert finished["state"] == "done"
  body = (harbor_env.root / "var" / "logs" / finished["log"]).read_text()
  assert "— ok" in body
  assert "live line" in body
  assert "done" in body


def test_activity_log_rejects_a_bad_name(harbor_env, client):
  assert client.get("/activity/nope.log").status_code == 404
  assert client.get("/activity/..%2F..%2Fetc%2Fpasswd").status_code == 404


def _install_cmd_demo(harbor_env):
  app_dir = harbor_env.main_repo / "cmd-demo.happ"
  app_dir.mkdir()
  (app_dir / "manifest.toml").write_text(
    '[app]\nversion = "1"\n\n'
    '[run.main]\nimage = "alpine:latest"\n'
    'cmd = ["/bin/sh", "-c", "sleep infinity"]\n\n'
    '[commands.ping]\ncmd = "echo pong"\ndesc = "Print pong"\n'
  )


def test_cmd_verb_runs_a_manifest_command_as_a_job(harbor_env, client, jobs):
  _install_cmd_demo(harbor_env)
  harbor_env.run("start", "cmd-demo")

  job = submit(client, jobs, "cmd", {"app": "cmd-demo", "command": "ping"})
  assert job["state"] == "done", job["error"]
  assert "ping" in read_log(job)

  # It reached docker as an exec of the declared argv, not something the caller
  # supplied.
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  assert any(call[:3] == ["compose", "exec", "main"] for call in calls)

  # And it filed activity like every other job.
  runs = client.get("/activity?app=cmd-demo").json()["activity"]
  assert runs[0]["verb"] == "cmd"
  assert runs[0]["status"] == "ok"


def test_cmd_verb_forwards_extra_arguments(harbor_env, client, jobs):
  _install_cmd_demo(harbor_env)
  harbor_env.run("start", "cmd-demo")

  job = submit(
    client, jobs, "cmd", {"app": "cmd-demo", "command": "ping", "args": "extra word"}
  )
  assert job["state"] == "done", job["error"]
  calls = [
    json.loads(line)["args"] for line in harbor_env.docker_log.read_text().splitlines()
  ]
  execs = [call for call in calls if call[:3] == ["compose", "exec", "main"]]
  assert execs
  assert execs[-1][-2:] == ["extra", "word"]


def test_cmd_verb_requires_a_command_argument(harbor_env, client):
  response = client.post("/jobs", json={"verb": "cmd", "args": {"app": APP}})
  assert response.status_code == 400
  assert "command" in response.json()["error"]


def test_cmd_verb_reports_an_unknown_command(harbor_env, client):
  harbor_env.run("start", "basic-features", "--set", "admin_user=root")
  response = client.post(
    "/jobs", json={"verb": "cmd", "args": {"app": "basic-features", "command": "nope"}}
  )
  assert response.status_code == 400
  assert "nope" in response.json()["error"]


def test_snapshots_empty_when_none_taken(harbor_env, client):
  assert client.get("/snapshots").json() == {"snapshots": []}


def test_snapshots_lists_archives_newest_first(harbor_env, client):
  snap = harbor_env.root / "snapshots" / "ports-demo"
  snap.mkdir(parents=True)
  (snap / "2020-01-01_00-00Z_old.tar.gz").write_bytes(b"x")
  (snap / "2024-06-15_12-00Z.tar.gz").write_bytes(b"z")
  (snap / "2026-01-01_00-00Z_new.tar.gz").write_bytes(b"y")
  body = client.get("/snapshots").json()["snapshots"]
  assert body == [
    {
      "app_id": "ports-demo",
      "name": "2026-01-01_00-00Z_new",
      "taken_at": "2026-01-01T00:00:00Z",
      "tag": "new",
      "bytes": 1,
    },
    {
      "app_id": "ports-demo",
      "name": "2024-06-15_12-00Z",
      "taken_at": "2024-06-15T12:00:00Z",
      "tag": "",
      "bytes": 1,
    },
    {
      "app_id": "ports-demo",
      "name": "2020-01-01_00-00Z_old",
      "taken_at": "2020-01-01T00:00:00Z",
      "tag": "old",
      "bytes": 1,
    },
  ]


def test_restore_verb(harbor_env, client, jobs):
  assert harbor_env.run("install", "ports-demo").returncode == 0
  taken = harbor_env.run("snapshot", "ports-demo", "--label", "back")
  assert taken.returncode == 0, taken.stderr
  name = Path(taken.stdout.split("written to ")[1].strip()).name.removesuffix(".tar.gz")
  job = submit(client, jobs, "restore", {"app": "ports-demo", "snapshot": name})
  assert job["state"] == "done", job["error"]
  assert "Restored" in read_log(job)


def test_restore_unknown_snapshot_is_refused(harbor_env, client):
  snap = harbor_env.root / "snapshots" / "ports-demo"
  snap.mkdir(parents=True)
  (snap / "2026-01-01_00-00Z_real.tar.gz").write_bytes(b"x")
  response = client.post(
    "/jobs",
    json={"verb": "restore", "args": {"app": "ports-demo", "snapshot": "nope"}},
  )
  assert response.status_code == 400
  assert "No snapshot nope" in response.json()["error"]


def test_restore_unknown_app_is_refused(harbor_env, client):
  response = client.post(
    "/jobs",
    json={"verb": "restore", "args": {"app": "never-snapshotted", "snapshot": "x"}},
  )
  assert response.status_code == 400
  assert "No snapshots found" in response.json()["error"]


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
  assert {
    "/apps",
    "/apps/{app_id}",
    "/catalog",
    "/jobs",
    "/jobs/{job_id}",
    "/snapshots",
  } <= set(paths)
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
  response = client.post("/jobs", json={"verb": "install", "args": {"app": app}})
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

  body = client.get("/volumes").json()
  volumes = {v["name"]: v for v in body["volumes"]}
  assert volumes["config"]["app_id"] == APP
  assert volumes["config"]["kind"] == "data"
  assert volumes["config"]["in_use"] is True
  assert volumes["config"]["declared"] is True
  assert volumes["config"]["bytes"] is None
  assert body["var_bytes"] is None
  assert body["snapshots_bytes"] is None
  assert body["repos_bytes"] is None

  media_dir = harbor_env.root / "external-data"
  media_dir.mkdir(exist_ok=True)
  (media_dir / "clip").write_bytes(b"abcd")
  record_volume_sizes(ctx())
  body = client.get("/volumes").json()
  volumes = {v["name"]: v for v in body["volumes"]}
  assert volumes["config"]["bytes"] == 2
  assert body["var_bytes"] > 0
  # repos/main holds the fixture happs, so it is gauged and non-empty.
  assert body["repos_bytes"] > 0
  media = {v["tag"]: v for v in client.get("/host-volumes").json()["host_volumes"]}
  assert media["media"]["bytes"] == 4


def test_volume_data_outliving_its_manifest_still_shows_up(harbor_env, client):
  """Re-staging drops the link of a volume the manifest stopped declaring and
  leaves the data. Nothing else would ever tell you it is still on disk."""
  assert harbor_env.run("start", APP, "--set", "admin_user=root").returncode == 0
  assert harbor_env.run("stop", APP).returncode == 0
  assert {v["name"] for v in client.get("/volumes").json()["volumes"]} == {
    "config",
    "cache",
  }

  manifest = harbor_env.main_repo / f"{APP}.happ" / "manifest.toml"
  # Dropped from [volumes] *and* from the unit that mounted it -- a manifest
  # that declares neither is what re-staging leaves data behind for.
  text = manifest.read_text().replace(
    'config = { kind = "data", desc = "persistent app data" }', ""
  )
  manifest.write_text(text.replace('config = "/myapp/config", ', ""))
  assert harbor_env.run("install", APP).returncode == 0

  volumes = {v["name"]: v for v in client.get("/volumes").json()["volumes"]}
  assert volumes["config"]["declared"] is False, "data left behind, flagged"
  assert volumes["cache"]["declared"] is True


# --- the single app view ---------------------------------------------------


def test_app_view_carries_what_a_detail_page_needs(harbor_env, client):
  assert harbor_env.run("install", APP).returncode == 0
  body = client.get(f"/apps/{APP}").json()

  unit = body["units"][0]
  assert unit["command"]
  # Placeholders, as the manifest wrote them. The *resolved* environment is
  # AppRunData.config_env and carries secret values; it must never appear.
  assert unit["environment"]["ADMIN_PASS"] == "${admin_pass}"

  assert body["metadata"]["display_name"] == "Basic Features"
  assert body["options"]["host_volumes"] == ["media", "other"]
  assert "none" in body["options"]["route_providers"]


def test_app_view_never_leaks_a_secret_through_the_environment(harbor_env, client):
  assert harbor_env.run("start", APP, "--set", "admin_user=root").returncode == 0
  _, secret = ctx().app_store(APP).get_config("admin_pass")
  assert secret
  assert secret not in json.dumps(client.get(f"/apps/{APP}").json())


def test_setting_config_through_the_api(harbor_env, client):
  assert harbor_env.run("install", APP).returncode == 0

  updated = client.post(f"/apps/{APP}/config", json={"set": {"admin_user": "alice"}})
  assert updated.status_code == 200, updated.text
  values = {c["name"]: c for c in updated.json()["config"]}
  assert values["admin_user"]["value"] == "alice"
  # The response is read back after the write, not from the request's context.
  assert not updated.json()["issues"]


def test_binding_a_host_volume_through_the_api(harbor_env, client):
  (harbor_env.root / "external-data").mkdir()
  assert harbor_env.run("install", "host-volumes").returncode == 0

  bound = client.post("/apps/host-volumes/config", json={"bind": {"hostvol1": "media"}})
  assert bound.status_code == 200, bound.text
  volumes = {v["name"]: v for v in bound.json()["volumes"]}
  assert volumes["hostvol1"]["bind"] == "media"


def test_config_changes_are_refused_with_a_reason(harbor_env, client):
  assert harbor_env.run("install", APP).returncode == 0

  for payload, expected in (
    ({"set": {"nope": "x"}}, "No config nope"),
    ({"bind": {"config": "media"}}, "only host volumes can be bound"),
    ({"route": {"main": "media"}}, "not declared"),
  ):
    refused = client.post(f"/apps/{APP}/config", json=payload)
    assert refused.status_code == 400, refused.text
    assert expected in refused.json()["error"]


def test_apps_and_catalog_agree_on_state(harbor_env, client, jobs):
  """One vocabulary across both views: no `staged`/`installed` booleans."""

  def catalog_entry():
    catalogs = client.get("/catalog").json()["catalogs"]
    return {app["app_id"]: app for app in catalogs[0]["apps"]}[APP]

  assert catalog_entry()["state"] == "available"
  assert client.get("/apps").json()["apps"] == []

  assert submit(client, jobs, "install", {"app": APP})["state"] == "done"
  app = client.get("/apps").json()["apps"][0]
  assert app["state"] == "installed"
  assert "staged" not in app
  assert catalog_entry()["state"] == "installed"
  # `status` is about containers and stays its own axis.
  assert app["status"] == "stopped"

  # /apps is the installed list; an uninstalled app is the catalog's to report.
  harbor_env.run("uninstall", APP, "-y")
  assert client.get("/apps").json()["apps"] == []
  assert catalog_entry()["state"] == "uninstalled"

  harbor_env.run("uninstall", "--purge", APP, "-y")
  assert client.get("/apps").json()["apps"] == []
  assert catalog_entry()["state"] == "available"


def test_uninstall_verb_keeps_data_and_config(harbor_env, client, jobs):
  harbor_env.run("start", APP, "--set", "admin_user=alice")
  data = harbor_env.volumes_root / "data" / APP / "config"
  (data / "app.db").write_text("rows")

  job = submit(client, jobs, "uninstall", {"app": APP})
  assert job["state"] == "done", job["error"]
  assert (data / "app.db").read_text() == "rows"
  assert harbor_env.app_logtab(APP).exists()
  assert client.get("/apps").json()["apps"] == []


def test_uninstall_purge_takes_everything(harbor_env, client, jobs):
  harbor_env.run("start", APP, "--set", "admin_user=alice")
  job = submit(client, jobs, "uninstall", {"app": APP, "purge": "1"})
  assert job["state"] == "done", job["error"]
  assert not (harbor_env.volumes_root / "data" / APP).exists()
  assert not harbor_env.app_logtab(APP).exists()


def test_reset_verb_clears_data_and_reinstalls(harbor_env, client, jobs):
  harbor_env.run("start", APP, "--set", "admin_user=alice")
  data = harbor_env.volumes_root / "data" / APP / "config"
  (data / "app.db").write_text("rows")

  job = submit(client, jobs, "reset", {"app": APP})
  assert job["state"] == "done", job["error"]
  assert list(data.iterdir()) == []
  assert (harbor_env.run_root / APP).is_dir()
  assert harbor_env.app_logtab(APP).exists()


def test_removal_verbs_refuse_an_unknown_app(harbor_env, client):
  for verb in ("uninstall", "reset"):
    response = client.post("/jobs", json={"verb": verb, "args": {"app": "nope"}})
    assert response.status_code == 400, verb


def test_a_freshly_started_app_has_nothing_pending(harbor_env, client):
  harbor_env.run("start", APP, "--set", "admin_user=alice")
  assert client.get(f"/apps/{APP}").json()["config_pending"] is False


def test_config_pending_is_false_when_not_running(harbor_env, client):
  """Nothing is pending on a stopped app: the next start reads config fresh."""
  harbor_env.run("install", APP)
  harbor_env.run("config", APP, "--set", "admin_user=alice")
  assert client.get(f"/apps/{APP}").json()["config_pending"] is False


def test_route_assignment_is_recorded_without_calling_the_provider(
  harbor_env, client, jobs
):
  submit(client, jobs, "install", {"app": "routes-demo"})
  response = client.post("/apps/routes-demo/config", json={"route": {"main": "web"}})
  assert response.status_code == 200, response.text
  assert client.get("/apps/routes-demo").json()["config_pending"] is False


# --- metrics ---------------------------------------------------------------


def _gauge_line(ago_s: int, name: str, value: str) -> str:
  ts = (
    (datetime.now(UTC) - timedelta(seconds=ago_s))
    .isoformat(timespec="seconds")
    .replace("+00:00", "Z")
  )
  return f"{ts}\tset\tgauge/{name}\t{value}\n"


def test_metrics_empty_when_nothing_recorded(harbor_env, client):
  body = client.get("/metrics").json()
  assert body["metrics"] == {}
  assert body["until"] - body["since"] == 3600


def test_metrics_returns_recent_gauge_history(harbor_env, client):
  c = ctx()
  c.record_gauge("host_cpu_used_ratio", 0.4)
  c.record_gauge("host_mem_used_ratio", 0.5)
  c.record_gauge("cpu_used_ratio/demo.app", 0.1)

  body = client.get("/metrics?prefix=host_cpu_used_ratio&hours=1").json()
  series = body["metrics"]["host_cpu_used_ratio"]
  assert len(series) == 1
  assert series[0]["v"] == 0.4
  assert "t" in series[0]
  assert "host_mem_used_ratio" not in body["metrics"]
  assert "cpu_used_ratio/demo.app" not in body["metrics"]


def test_metrics_drops_points_older_than_hours(harbor_env, client):
  c = ctx()
  with c.config.metrics_log.open("a") as f:
    f.write(_gauge_line(7200, "host_cpu_used_ratio", "0.9"))
  c.record_gauge("host_cpu_used_ratio", 0.2)

  body = client.get("/metrics?prefix=host_cpu_used_ratio&hours=1").json()
  assert [p["v"] for p in body["metrics"]["host_cpu_used_ratio"]] == [0.2]


def test_metrics_refuses_hours_below_one(harbor_env, client):
  response = client.get("/metrics?hours=0")
  assert response.status_code == 400
  assert "hours must be >= 1" in response.json()["error"]
