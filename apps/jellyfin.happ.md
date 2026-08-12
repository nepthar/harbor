# Jellyfin - Media server

> Jellyfin is the volunteer-built media solution that puts you in control of
> your media. Stream to any device from your own server, with no strings
> attached. ~ https://jellyfin.org

Jellyfin presents and streams your media library. Also consider auditing
where you place your "bulk" data - this harbor app uses a bulk volume to 
place metadata.

## manifest.toml
```toml happ_path="manifest.toml"
[app]
version      = "1.0.0"
display_name = "Jellyfin Media Server"
description  = "Stream your own movies, shows and music to any device"
subdomain    = "jelly"

[volumes]
config   = { kind = "data", desc = "Server config, users, playback state" }
metadata = { kind = "bulk", desc = "Artwork, trickplay images, subtitles" }
cache    = { kind = "temp", desc = "Transcode and image cache; safe to lose" }
media    = { kind = "host",  desc = "The library itself; bind to the media share", readonly = true }

[run.main]
image   = "jellyfin/jellyfin"
volumes = { config = "/config", metadata = "/metadata", cache = "/cache", media = "/media" }

[run.main.routes]
# Pinned rather than harbor-allocated: TVs and phones already point at :8096,
# and jellyfin's own autodiscovery advertises that port.
main = { port = "8096:8096" }

[run.main.compose]
# Run as the owner of the media share rather than root.
user = "1000:1000"

[run.main.env]
# Autodiscovery hands clients this address instead of the container's own.
# harbor fills it in from the `main` route once the app is staged.
JELLYFIN_PublishedServerUrl = "${routes.main}"
```

## Installing

The media share has to be mounted on the host first — harbor binds a directory,
it does not speak NFS. Mount your media through fstab, autofs, or
a systemd `.mount` unit, declare it in config.toml, then bind the app volume:

```toml
[host_volume.media]
path = "/mnt/my-media"
readonly = true
```

```
harbor config jellyfin --bind media=media
harbor start jellyfin
```

Until that bind exists, `harbor ps` reports jellyfin as needing config and
refuses to start it.

Note that at the moment, this app does not support hardware encoding/decoding.
You may enable it by adding the following key:
```toml
[run.main.compose]
devcies = [ "/dev/dri/renderD128:/dev/dri/renderD128", "/dev/dri/card0:/dev/dri/card0" ]
```
