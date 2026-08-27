"""Harbor — happ runtime and tooling."""

from harbor.lib.util import name_log_levels

VERSION = "0.1.0"

# Both entrypoints and the library write to `harbor.*` loggers, and their
# records end up in the same run logs. Naming the levels here rather than in
# either entrypoint is what keeps that one format.
name_log_levels()
