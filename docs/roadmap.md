# Roadmap

## Audience
This roadmap is based on the desire to become a tool that someone technical enough to install linux on an old machine, and can paste in terminal commands would reach for. We want more people to run self hosted stuff.

Kubernetes and similar can be overkill, while raw Docker Compose files alone still leave missing pices of the puzzle.

## Features
Our feature roadmap, roughly in order of priority

### Systemd unit and one-command installer
At the moment, `harbord` runs foreground-only. We need persistence and installability

### live container logs
`docker compose logs -f` in the webui

### Cron Jobs
Add the ability to define and execute commands and cron jobs from within the manifest.toml. Think regular admin tasks, database cleanup, password reset, etc.

### Use and surface docker health checks
Right now, we just check to see if the container is running

### "Lambda Function" apps
Default state - not running, but can be started triggered on a cron or incoming http request

### Automated snapshot backup strategy
Use cron to copy snapshot bundles somewhere

### First run setup wizard

### Manifest editing and validation from the webui

### Harbor config.toml editing and validation from the webui

### Services & Service Catalog
Allow happ developers to better focus on their own app by saying "Just give me a postgres instance + login for my app" rather than adding postgres to their stack manually. This `services` system would enable an happ to list the services it "provides" and have other services "require" them. This feature will require a lot of thought.


## Other issues the LLMs find:
- **A `cmd` job holds the app lock for the command's whole run.** Harbor-wide
  ops can proceed; the same app cannot be staged, started, or stopped until it
  exits. Fine for the batch-style commands the UI is for; a long-runner still
  wedges that app. The runner also allocates no TTY, so a command that waits
  on stdin hangs rather than prompting.
- **Only daemon jobs file activity output.** A CLI invocation prints to the
  operator's terminal and records only its status line in `activity.logtab`,
  so the UI's Activity page shows what harbord ran, not what the operator
  typed. The mechanism to close this is in place — `Job.call(args, ctx,
  echo=stream)` writes the run log and the terminal from one stream — and the
  plan is to migrate CLI verbs onto their Job classes, verb by verb.
- **harbor-ui has no tests.** It is HTML builders and FastAPI routes with no
  TestClient coverage, and it has grown: a dashboard that now carries the app
  list, the Repos page, compose warnings, and the stale-manifest notices.
  Every one of those is a pure function from an API payload to a string, which
  is the easy half to cover.
- **Rootless docker breaks snapshot and restore silently.** `lib/lifecycle/
  rootfs.py` assumes the container's root can read root-owned volume files;
  under userns that maps back to the invoking user. Nothing checks.
