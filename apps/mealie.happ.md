# Mealie - Recipe manager

> Mealie is a self hosted recipe manager, meal planner and shopping list with a RestAPI backend and a reactive frontend built in Vue for a pleasant user experience for the whole family. Easily add recipes into your database by providing the URL and Mealie will automatically import the relevant data, or add a family recipe with the UI editor. Mealie also provides an API for interactions from 3rd party applications. ~ https://github.com/mealie-recipes/mealie/

This harbor app installs mealie v3.22.0 with the sqlite backend.

Mealie builds absolute links (password resets, shared recipes) from `BASE_URL`,
so it has to be told the address it answers on. `${routes.main}` is that
address: the URL harbor publishes the `main` route at, which is only knowable
once the app is staged and the route is allocated.

```toml happ_path="manifest.toml"
[app]
version      = "3.22.0"
display_name = "Mealie Recipe Manager (sqlite)"
description  = "Manage, save, share recipes and make shopping lists"
subdomain    = "mealie"

[config]
timezone = { desc = "The timezone of the host system", default = "UTC" }

[volumes]
data = { kind = "data", desc = "sqlite database, mealie state" }

[run.main]
image   = "ghcr.io/mealie-recipes/mealie:v3.22.0"
volumes = { data = "/app/data" }

[run.main.routes]
main = { port = "9000", publish = "web" }

[run.main.compose]
# Upstream's compose asks for this; mealie's importer is memory hungry.
mem_limit = "1000m"

[run.main.env]
ALLOW_SIGNUP = "false"
PUID         = "1000"
PGID         = "1000"
TZ           = "${timezone}"
BASE_URL     = "${routes.main}"
```
