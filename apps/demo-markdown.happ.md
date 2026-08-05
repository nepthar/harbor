# Markdown Demo Harbor app

This document is a valid harbor app.

Since one of the core properties of harbor is being easy to audit, I wanted a way to distribute single-file complete apps, as long as they're small enough. Bundle folders (ending in .happ) will always be supported, but this provides an at-a-glance audit to potential users for apps.

## How it works.
Markdown already supports code blocks of arbitrary languages, and any text after the language identifier is discarded. As such, we add another identifier after the type, which is used to extract the contents of the quote block. It looks like `happ_path="path/to/my/file.py"` However, the path isn't visible to someone just reading the markdown, so that must be repeated in the document somewhere.

You may add as little or as much document around this. Harbor will ignore it and just extract every code block that has a `happ_path` tag on it.

## Demo App
Below is the demo app itself. Since we're reproducing these files onto a real filesystem from here, if you want to mark a script as executable, add ":+x" to the end of the filename.

### manifest.toml
Below is the complete manifest, which will be extracted to `manifest.toml` when this app is staged.
```toml happ_path="manifest.toml"
[app]
version      = "0.1.0"
display_name = "Markdown showcase"
description  = "Demonstrates using markdown to distribute a harbor app"

[volumes]
hello_script = { kind = "app", src = "hello.sh", desc = "The hello script from the markdown document" }

[run.main]
image   = "alpine:latest"
cmd     = ["/bin/sh", "-c", "/app/hello.sh"]
volumes = { hello_script = "/app/hello.sh" }
restart = "no"
```

### hello.sh
```bash happ_path="hello.sh:+x"
echo "Hello world"
echo "it is now"
date
echo "Goodbye!"
```

You can confidently run this harbor app, knowing everything about it in about 15 seconds.
