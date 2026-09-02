# Harbor UI

Server-rendered HTML over the harbord admin socket. No client framework, no
build step. `ui/layout.py` owns the page chrome; the other modules emit
fragments into it. Read the root `AGENTS.md` first — everything there applies.

## The visual language

**Commit to a few decisions and apply them everywhere.** A small set of
specific choices, held without exception, is what makes an interface look
considered. Reaching for a new value because this one spot wants it is what
makes it look assembled. When a screen needs something the language doesn't
have, say so and we'll add it to the language — don't add it locally.

**Every style lives in `static/harbor.css`.** No per-page `<style>` blocks, no
inline `style=` attributes, no color or size literals in page modules. Pages
emit semantic class names; if the class doesn't exist yet, add it to the
stylesheet.

**Build with the tokens, never past them.** If you type a hex value, a radius,
or a font stack anywhere outside `:root`, you have gone around the design.

| Token | Job |
|---|---|
| `--void` | The gutter behind the app frame. Also the well inside inputs and `<pre>`. |
| `--bg` | The ground everything sits on. |
| `--panel` | Raised: wells, modals, notices. Reach for it only when something must lift off the ground. |
| `--line` / `--hair` | A hairline that separates / one that barely does (table rows). |
| `--fg` / `--dim` / `--muted` | Content / labels and secondary data / metadata and disabled. Three weights, one job each, at roughly 14:1, 9:1 and 5.5:1 against `--bg`. Recessive is a step down this ladder, never a step below it — `--muted` is the floor, and it still has to be legible in a bright room. |
| `--coral` | The one accent. Actionable, active, focused. |
| `--gold` `--rosewood` `--ok` | Attention / destructive and failed / healthy. |
| `--ink` | Type on a warm fill. Dark type on coral, never white. |
| `--r` | The radius. There is one. |

**Hierarchy comes from type and space before it comes from boxes.** A new
group starts as an `h2` section label — small, lowercase, with a rule running
to the right edge — and nothing else. `.card` is a pair of hairlines around a
table. Only `.card.pad` fills, and only when the content is a well rather than
a list. If two adjacent things both have borders, one of them is wrong.

**Color carries meaning, never decoration.** Coral marks what you can act on.
Gold wants attention, rosewood destroys, olive is healthy. Nothing on the page
is colored for interest.

**Actions sit beside the title, not at the far edge.** `.head-actions` follows
the `h1` and `Refresh` is pushed right, so a page's verbs are inside the
reading path instead of a screen-width away from it. Where exactly one of a
pair can ever apply — start/stop — draw that one and leave the other out
rather than rendering it disabled.

**Page actions are words; row actions are icons.** A verb beside the `h1` is
read once and has room for a label, so it gets one. A verb repeated down every
row of a table would turn a column into a wall of the same word, so it gets a
glyph and a `title`. Label an action for what it is about to do, not for the
outcome you expect: `Remove` opens a chooser of uninstall, reset and purge, and
naming it any one of those would be a lie.

**One filled control per surface, and only where the surface asked for
something.** A modal, a config row and an app card each have a thing you came
to do — Run, Save, Install — and that control is filled coral with `--ink`. A
page that shows you an app has no such thing, so nothing on it is filled;
filling its lifecycle verb tells the reader the page wants them to stop the
app, which it doesn't. Every other control is a hairline button with `--dim`
type that brightens on hover, and destructive ones take `danger=True` on
`job_button` / `icon_button` for a rosewood hover.

**Mono is structural, not stylistic.** Identifiers, versions, sizes, paths,
ports, counts, durations and timestamps are IBM Plex Mono, so columns of them
line up down the page. Prose — descriptions, hints, notices, empty states — is
never mono. In tables this falls out of `td.muted:not(.wrap)`; keep prose
cells classed `.wrap`.

**Section labels and nav are lowercase.** Page titles (`h1`) and content
(app names, descriptions) keep their own capitalization.

**Square geometry.** `--r` is 2px, status markers are squares, and the app
frame's top-left corner is chamfered. Nothing in this UI is round.

**The ribbon is the identity and it appears exactly twice**: cutting the
chamfered corner, and flattened onto the left end of the rule under the page
title, where `.head::after` centres it on the 1px border and so takes no layout
space of its own. The corner's geometry is load-bearing — `--chamfer` must stay
wide enough to clear the band, whose centerline runs `(0, C) → (C, 0)` for
`C = --ribbon-top + --ribbon/2`. Change one, check the other. Don't introduce
a third instance.

**Every interactive element gets a visible `:focus-visible` ring** in coral.

**Layout rhythm comes off the scale**: 4 / 8 / 12 / 16 / 20 / 24 / 32 / 48,
for margins between blocks, flex gaps and section spacing. Padding inside a
control is optical, tuned once on the component in the stylesheet — read it off the
existing rule rather than re-tuning it at the call site.

## Working on it

Run it against your real harbor rather than guessing — it takes two commands
and it is the only way to see whether a change landed:

```
HARBOR_ROOT=$HOME/harbor uv run harbord --port 9797 --host 127.0.0.1
cd apps/harbor-ui.happ/ui && HARBOR_API=127.0.0.1:9797 ADMIN_PASSWORD=dev \
  ./.venv/bin/uvicorn server:app --port 9798 --reload
```

`ADMIN_PASSWORD` is not optional — `auth.py` refuses to import without one.
Plain http is fine here: the session cookie only asks to be `Secure` when the
request that minted it arrived over TLS, which in the container it does.

Then look at every page you touched — `/`, `/apps`, `/apps/<id>`, `/volumes`,
`/catalog`, `/logs`, `/snapshots`, `/login` — plus the collapsed nav and one
modal. Layout regressions here are invisible in a diff and obvious on screen.
