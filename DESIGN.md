---
name: mcpelevator
description: A dark, quiet operator panel for supervising self-hosted MCP servers.
colors:
  signal-emerald: "#34d399"
  signal-emerald-deep: "#10b981"
  emerald-ink: "#022c22"
  canvas: "#09090b"
  surface: "#101012"
  surface-2: "#161619"
  elevated: "#1c1c20"
  line: "#26262b"
  line-strong: "#34343a"
  ink: "#f4f4f5"
  ink-muted: "#a1a1aa"
  ink-dim: "#87878f"
  state-running: "#34d399"
  state-starting: "#fbbf24"
  state-unhealthy: "#fb923c"
  state-failed: "#f87171"
  state-stopped: "#87878f"
  state-idle: "#60a5fa"
typography:
  headline:
    fontFamily: "'Inter var', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 600
    letterSpacing: "-0.025em"
  title:
    fontFamily: "'Inter var', ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
  body:
    fontFamily: "'Inter var', ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.43
  label:
    fontFamily: "'Inter var', ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    letterSpacing: "0.025em"
  mono:
    fontFamily: "ui-monospace, 'SF Mono', 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace"
    fontSize: "0.75rem"
    fontWeight: 400
rounded:
  sm: "6px"
  md: "8px"
  card: "14px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  page-gutter: "16px"
  page-gutter-wide: "24px"
components:
  button-primary:
    backgroundColor: "{colors.signal-emerald}"
    textColor: "{colors.emerald-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  button-primary-hover:
    backgroundColor: "{colors.signal-emerald-deep}"
    textColor: "{colors.emerald-ink}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
    height: "36px"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.md}"
    padding: "6px 12px"
  card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.card}"
    padding: "20px"
  input:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  runner-badge:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.mono}"
    rounded: "{rounded.sm}"
    padding: "2px 6px"
  menu:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.md}"
    padding: "4px 0"
---

# Design System: mcpelevator

## Overview

**Creative North Star: "The Quiet Switchboard"**

mcpelevator looks like an operator's switchboard at night: a near-black zinc panel with rows of identical units, where a small green light says a line is live and everything else stays dim until it needs a hand. Most visits are check-ins, so a healthy instance should read as calm, and the eye should land only on what has changed state or needs an action.

It is a dense, flat, dark-only interface. Depth comes from stepping the zinc surface a shade lighter, not from shadows. Color is reserved for meaning: the lifecycle state of a server, and the one primary action on a screen. Technical identity (slugs, URLs, runners, commands) is set in mono, so a glance tells "a name a human chose" apart from "a string a machine routes on". The only atmosphere is a faint emerald glow (7%) falling from the top of the page, behind everything.

Components are quiet and exact: hairline borders, 8px corners, a one-pixel press on click, and no decoration that doesn't carry state.

**Key Characteristics:**
- Dark only (`color-scheme: dark`), on a warm-neutral zinc ramp.
- One accent, Signal Emerald, meaning live/healthy and marking the primary action. No gradients except the ambient top glow.
- Lifecycle state colors are fixed semantics and always come with a text label.
- Mono for machine identifiers, Inter for everything a human reads.
- Flat surfaces with tonal layering. Shadows only on things that float (menus, dialogs, toasts).
- Small type, tight rhythm: most UI text is 12–14px.

## Colors

A near-black zinc ramp holding a single emerald signal, plus a fixed set of lifecycle state hues.

### Primary
- **Signal Emerald** (#34d399): the "live" light. Running state, the healthy dot, the active nav item (40% border, 8% fill), focus rings, text selection (30%), chart series, and the fill of the primary action in each decision (Add server, Start, Save, Install).
- **Deep Emerald** (#10b981): hover for primary filled buttons only.
- **Emerald Ink** (#022c22): text and icons on an emerald fill. Never used on its own.

### Neutral
- **Base Night** (#09090b, token `--color-canvas`): page background. The token is deliberately not named `base`: Tailwind would read `text-base` as this color instead of the 16px size.
- **Panel** (#101012): cards, secondary buttons, the resting layer of content.
- **Panel Raised** (#161619): inputs, badges, hover fills inside cards, code wells.
- **Floating** (#1c1c20): menus, popovers, dialogs, anything above the page.
- **Hairline** (#26262b): default borders and dividers.
- **Hairline Strong** (#34343a): hovered card borders, menu borders, focused input borders.
- **Ink** (#f4f4f5): primary text and headings.
- **Ink Muted** (#a1a1aa): secondary text, resting button labels, menu items.
- **Ink Dim** (#87878f): slugs, help text, placeholders, timestamps, icon buttons at rest.

### Lifecycle states
Fixed meanings, shared by every surface (pills, dots, chart markers, log highlights):
- **Running**: Signal Emerald (#34d399).
- **Starting / Stopping / startup phases**: Amber (#fbbf24), with a pulsing dot.
- **Unhealthy**: Orange (#fb923c).
- **Failed**: Coral Red (#f87171).
- **Stopped**: Zinc (#87878f).
- **Idle** (deliberately quiesced, wakes on request): Calm Blue (#60a5fa). Calm, not alarming.

State tints are built with `color-mix(in oklab, <state> N%, transparent)`: about 12% for a fill, 35–40% for a border, the full color for text and dots.

### Named Rules
**The One Signal Rule.** Signal Emerald means live, healthy, active or "do this". It is never decoration and never a heading color. Each decision gets at most one filled emerald button: a page header has one, and every card or row in a list of equals may have its own (Start on a stopped server, Install on a catalog entry). Two filled emerald buttons competing for the same decision is the violation.

**The Label-Always Rule.** A state color never appears without its word. A dot sits inside a pill that says "Running", "Failed" or "Idle", so meaning survives color-blindness and grayscale.

**The Tint, Don't Paint Rule.** Status surfaces are tinted (`color-mix` at about 12% fill and 35% border), never solid blocks of state color. Solid emerald is reserved for the primary action.

**The Dim Floor Rule.** Ink Dim (#87878f) is the dimmest color text may use, and it clears 4.5:1 on every surface up to Floating (#1c1c20) and on emerald tints. Never set text in Hairline or any value darker than Ink Dim. Fade decoration, not words.

## Typography

**Body Font:** Inter var (with ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto fallbacks), stylistic alternates `cv02 cv03 cv04 cv11` on.
**Mono Font:** ui-monospace (SF Mono, JetBrains Mono, Fira Code, Menlo, Consolas).

**Character:** one neutral grotesk set small and tight, with a system mono for anything a machine reads. There is no display face. Hierarchy comes from weight and a few size steps, not from contrast between families.

### Hierarchy
- **Headline** (600, 1.5rem/24px, tracking -0.025em): page titles ("Servers", "Usage", "Settings"). One per page.
- **Title** (600, 15px): card titles (server names), section heads inside a page.
- **Body** (400–500, 0.875rem/14px): form labels (500), button labels (500–600), menu items, paragraph copy.
- **Label** (500, 0.75rem/12px, tracking +0.025em): state pills, help text under fields, metadata rows. This is the most-used size in the app.
- **Micro** (500, 11px): runner badges, chart axes, heatmap hour labels, dense table metadata. 11px is the floor for any text a person needs to read; sentences (captions, help, notes) are never smaller than 12px.
- **Mono** (400–500, 11–12px): slugs, endpoint URLs, runner names, commands, env keys, log lines.

### Named Rules
**The Machine-in-Mono Rule.** If a string is routed on, copied into a config or typed into a terminal (slug, URL, command, env key, tool name), it is mono. If a human named it, it is Inter.

**The Small-Steps Rule.** The scale stays between 11px and 24px. New hierarchy comes from weight (500/600) and Ink vs Ink Muted vs Ink Dim, not from new sizes.

## Layout

A single centered column, at most 72rem (1152px) wide (`max-w-6xl`), with 16px side gutters on phones and 24px from `sm` up. Narrow tasks such as forms and login use tighter columns (`max-w-2xl`, `max-w-md`). A sticky 56px top bar (Base Night at 85% with a backdrop blur, Hairline bottom border) holds the logo on the left and nav on the right. Below `sm`, nav items collapse to icon-only 36px squares.

The dashboard is a responsive card grid: one column on phones, two from `sm`, three from `lg`, with a 16px gap. Each page opens with a header row: headline plus a muted one-line summary ("5 of 6 running") on the left, actions on the right.

Spacing is a 4px-based Tailwind rhythm, tight by default: 6–8px between inline siblings (`gap-1.5`/`gap-2`), 12–16px between grouped blocks, 20px card padding. Controls are 32–36px tall. The footer (GitHub link · version) sits at the bottom of a 100dvh flex column.

## Elevation & Depth

The system is flat and uses tonal layering. Resting content sits one step above the page (Panel on Base Night), and interactive wells sit one step further (Panel Raised). Hairline borders separate everything. Hovering a card brightens its border to Hairline Strong instead of lifting it.

### Shadow Vocabulary
- **Float** (`box-shadow: 0 25px 50px -12px rgb(0 0 0 / 0.25)`, Tailwind `shadow-2xl`): menus, popovers, dialogs and toasts only, always on the Floating surface with a Hairline Strong border.

### Named Rules
**The Flat-Until-Floating Rule.** Nothing in the page flow casts a shadow. A shadow means the element sits above the page and will go away (a menu, dialog or toast).

## Shapes

Soft, consistent rectangles. Controls (buttons, inputs, nav items, menus) use an 8px radius, the workhorse corner. Small inline objects (runner badges, icon buttons, file-input buttons) use 6px. Toasts use 12px. Cards and large containers use a 14px card radius, so a card reads as a softer container around sharper controls. State pills and dots are fully round. Borders are always 1px. There are no clipped corners, no angled cuts and no decorative silhouettes.

## Components

### Buttons
Quiet and exact: flat, labeled, a 1px `translateY` press on `:active`, color transition on hover.
- **Shape:** 8px radius (`rounded-lg`).
- **Primary:** Signal Emerald fill, Emerald Ink text, 600 weight, 8px × 12px padding (14px label), hovering to Deep Emerald. One per decision: one in a page header, one per card or row in a list of equals (Add server, Start, Save, Install).
- **Secondary:** Panel fill, 1px Hairline border, Ink Muted label, 36px tall. Used for Refresh, nav items and Copy. When it is the current nav item it takes the active tint (Signal Emerald text, 40% emerald border, 8% emerald fill).
- **Quiet / Stop:** transparent fill, Hairline border, Ink Muted label. Used for destructive-but-routine lifecycle actions (Stop), so stopping never shouts.
- **Busy:** an inline 14–16px spinner replaces the icon, the label becomes a gerund ("Starting", "Stopping"), and the button sets `aria-busy` and shows a wait cursor at 70% opacity.
- **Icon-only:** 36px square, or a 4px-padded ghost (the card kebab), Ink Dim brightening to Ink on a Panel Raised hover. Always carries an `aria-label`.

### State Pill (signature)
The heart of the system. A fully round pill: 12px label at 500 weight with +0.025em tracking, 4px × 10px padding, and a 6px leading dot. Text and dot use the state color, with a 12% state fill and 35% state border. Transient states (Starting, Stopping, startup phases such as "Installing") pulse the dot (opacity 1 → 0.35 over 1.4s, ease-out-quint).

### Runner Badge
A small mono tag naming how a server runs (`npx`, `uvx`, `cmd`, `docker`, `remote`, `openapi`, `built-in`): 11px mono at 500 weight, Panel Raised fill, Hairline border, Ink Muted text, 6px radius, 2px × 6px padding. Exposure tags (`MCP`, `REST`) use the same shape tinted emerald.

### Cards
- **Corner Style:** 14px card radius.
- **Background:** Panel (#101012) on Base Night.
- **Shadow Strategy:** none (see Flat-Until-Floating).
- **Border:** 1px Hairline, Hairline Strong on hover.
- **Internal Padding:** 20px, with 16px between header, badges and footer. The footer is separated by a Hairline divider and holds Copy on the left and the lifecycle action on the right.

### Inputs / Fields
- **Style:** Panel Raised fill, 1px Hairline border, 8px radius, 8px × 12px padding, 14px Ink text, Ink Dim placeholder.
- **Focus:** the border brightens to Hairline Strong, and keyboard focus adds the global 2px Signal Emerald outline at a 2px offset.
- **Labels and help:** a 14px/500 Ink label above, 12px Ink Dim help text below, 8px apart.
- **Toggles:** a visually hidden checkbox plus a styled peer track. Never a bare native checkbox in forms.

### Navigation
A sticky top bar: the logo (mark plus the lowercase wordmark with "mcp" in Signal Emerald) on the left, then Browse, Usage, Settings and an API status dot on the right. Every item uses the one `.nav-item` class in `app.css` (36px tall, secondary-button styling), with an icon and a label that collapses to icon-only below `sm`. The current page is marked with `aria-current="page"`, and the emerald active tint is keyed off that attribute, so the tint and the semantics can't drift apart. The API health dot is solid (no ping); its label is visible from `sm` and screen-reader-only below it.

### Endpoint copy controls
The URL is what people come for, so copying it has one vocabulary everywhere. **Copy URL** copies the bare endpoint; **Add to client ▾** opens the per-client menu (Claude Code, Desktop, web/mobile, Codex, ChatGPT, Gemini CLI, VS Code), whose first item is the bare MCP URL. The server page shows both; a dashboard card shows only **Add to client ▾** to keep its footer on one line. The menu is a disclosure (`aria-expanded` + `aria-controls`), not an ARIA menu.

### Ready strip (signature)
When the server page watches a start finish, the amber startup panel is replaced in the same slot by a Running-tinted strip: **Ready · N tools**, a one-line prompt, Copy URL, Add to client, and a dismiss ×. It enters with `rise-in` (a fade under reduced motion) and sits above later siblings (`z-20`) so its menu isn't covered. A visually hidden `aria-live="polite"` line announces each phase, readiness ("… is ready, N tools.") and failure. During the first seconds of a readiness phase the probe's error message is held back behind "Waiting for the server to answer…"; the countdown reads "Gives up in …" / "Retrying in …".

### Endpoint notes
Below the URLs, one quiet line per fact, each with a 14px stroke icon in Ink Muted and Ink Dim text, never a warning color: the auth requirement (or "No auth: anyone who can reach this URL can call its tools."), reachability when the URL is loopback/private/`.local` or plain http (pointing at `MCPE_PUBLIC_BASE_URL`), and "Stopped" / "Idle" consequences. The Endpoints card keeps its position in every state; while starting or failed, logs sit directly under it.

### Disclosures in forms
Settings a first server can leave at defaults live in collapsed groups on the Panel surface with a Hairline border: **Advanced** (working directory, setup script, raw launch config) and **Exposure & access** (exposure toggles, idle shutdown, auth). Each closed group's second line states its current values in mono (`MCP · auth: Settings default · idle: default`), so collapsing hides nothing. Native `<details>` keeps fields mounted; a group opens itself when a field inside it is invalid. On phones the form's actions stick to the bottom of the screen, and a disabled submit always says why next to it ("Add the npm package.").

### Menus
A Floating surface (#1c1c20) with a Hairline Strong border, 8px radius, 4px vertical padding and the Float shadow. Items are 14px Ink Muted rows (8px × 12px) with a 16px stroke icon, filling with Panel Raised and Ink on hover. A destructive confirmation replaces the item list in place rather than opening a second dialog.

### Charts
LayerChart, themed through CSS variables so charts reuse these tokens: the primary series is Signal Emerald, and chart surfaces map to Panel, Panel Raised and Hairline. Fixed-geometry visuals (the usage punchcard, proportional row bars) are plain DOM in the same tokens.

## Do's and Don'ts

### Do:
- **Do** pair every state color with its word, using the State Pill or its dot-plus-label pattern.
- **Do** build state tints with `color-mix(in oklab, <state> 12%, transparent)` fills and 35–40% borders.
- **Do** set slugs, URLs, commands, env keys and tool names in mono at 11–12px.
- **Do** step depth through Base Night → Panel → Panel Raised → Floating, and separate with 1px Hairlines.
- **Do** keep one filled Signal Emerald button per decision (the page header, or each card or row in a list of equals), and make it the action that decision exists for.
- **Do** use the global 2px Signal Emerald focus outline (2px offset) for every interactive element.
- **Do** show busy work in place: a spinner, a gerund label, `aria-busy`.

### Don't:
- **Don't** introduce a second accent hue or use emerald as decoration, heading color or illustration fill.
- **Don't** use gradients beyond the existing ambient top glow.
- **Don't** put shadows on cards or anything in the page flow. Shadows are only for floating layers.
- **Don't** make a light theme variant ad hoc. The system is dark only (`color-scheme: dark`).
- **Don't** render Stop or other routine lifecycle actions as red or filled. They stay quiet.
- **Don't** add type sizes above 24px or display faces to the app UI.
- **Don't** show Idle in a warning color. It is a deliberate, healthy state (Calm Blue).
- **Don't** add an animation without a `prefers-reduced-motion` path. Under reduced motion, pulses hold a steady dimmed dot, entrances fade in 0.2s, skeleton shimmer stops, and spinners keep turning more slowly.
- **Don't** add an endless loop to a healthy state. Motion marks something in progress (starting, stopping, loading), never "all is well".
