---
version: alpha
name: Eidolon Control Plane
description: A dark-first, local-first interface for deliberate AI work, reusable skills, approvals, schedules, and trusted applications.
colors:
  primary: "#5B82C4"
  secondary: "#2A3C4E"
  tertiary: "#BFD0EC"
  canvas: "#060A0E"
  sidebar: "#080D11"
  topbar: "#04070A"
  panel: "#0D1419"
  surface: "#10161B"
  surface-hover: "#0F171E"
  surface-selected: "#1A2631"
  line: "#20272D"
  line-strong: "#283038"
  text: "#ECECE6"
  text-muted: "#B3BBC3"
  text-secondary: "#98A2AC"
  text-tertiary: "#7E8993"
  button-fill: "#E7EBEF"
  button-text: "#0A0D10"
  focus: "#83A2FF"
  success: "#61B095"
  warning: "#E2A14C"
  error: "#FF8A8A"
  running: "#7CA9FF"
  light-primary: "#245FA5"
  light-secondary: "#D2DCE8"
  light-tertiary: "#DBE7F5"
  light-canvas: "#FFFFFF"
  light-sidebar: "#EEECE7"
  light-panel: "#F7F6F3"
  light-surface-selected: "#E4E8ED"
  light-line: "#E5E7EB"
  light-line-strong: "#D9D9DD"
  light-text: "#212121"
  light-text-muted: "#51565C"
  light-text-secondary: "#686F78"
  light-text-tertiary: "#7B828B"
  light-focus: "#4C6EE6"
  light-success: "#2F7D67"
  light-warning: "#B76F20"
  light-error: "#B30000"
  light-running: "#1863DC"
typography:
  headline-display:
    fontFamily: Plus Jakarta Sans
    fontSize: 3.25rem
    fontWeight: 400
    lineHeight: 1.03
    letterSpacing: -0.025em
  headline-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 1.9rem
    fontWeight: 400
    lineHeight: 1.15
    letterSpacing: -0.025em
  headline-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 1.5rem
    fontWeight: 400
    lineHeight: 1.25
    letterSpacing: -0.025em
  headline-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 1.08rem
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: -0.025em
  body-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.5
    wordSpacing: 0.04em
  body-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
  label-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.4
  metadata-md:
    fontFamily: IBM Plex Mono
    fontSize: 10px
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: 0.1em
  metadata-sm:
    fontFamily: IBM Plex Mono
    fontSize: 9px
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: 0.08em
  code-sm:
    fontFamily: IBM Plex Mono
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.58
rounded:
  none: 0px
  xs: 1px
  sm: 4px
  md: 6px
  lg: 8px
  bubble: 14px
  full: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
  2xl: 48px
  page-x: "clamp(20px, 3.5vw, 44px)"
  page-top: 34px
  page-bottom: 60px
  sidebar-width: 220px
  content-max: 1280px
components:
  app-shell:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.text}"
    typography: "{typography.body-md}"
  sidebar:
    backgroundColor: "{colors.sidebar}"
    textColor: "{colors.text-secondary}"
    width: "{spacing.sidebar-width}"
    padding: 24px 16px 16px
  topbar:
    backgroundColor: "{colors.topbar}"
    textColor: "{colors.text}"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.none}"
  surface:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-muted}"
  row-hover:
    backgroundColor: "{colors.surface-hover}"
    textColor: "{colors.text-secondary}"
  row-selected:
    backgroundColor: "{colors.surface-selected}"
    textColor: "{colors.text}"
  divider:
    backgroundColor: "{colors.line}"
    height: 1px
  divider-strong:
    backgroundColor: "{colors.line-strong}"
    height: 1px
  button-primary:
    backgroundColor: "{colors.button-fill}"
    textColor: "{colors.button-text}"
    typography: "{typography.label-md}"
    rounded: "{rounded.sm}"
    padding: 8px 16px
    height: 32px
  button-primary-hover-wave:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.button-text}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: 8px 16px
    height: 32px
  navigation-active-indicator:
    backgroundColor: "{colors.primary}"
    rounded: "{rounded.xs}"
    width: 3px
    height: 16px
  interaction-subtle:
    backgroundColor: "{colors.secondary}"
    textColor: "{colors.text}"
  text-muted:
    textColor: "{colors.text-muted}"
    typography: "{typography.body-sm}"
  text-secondary:
    textColor: "{colors.text-secondary}"
    typography: "{typography.body-sm}"
  text-tertiary:
    textColor: "{colors.text-tertiary}"
    typography: "{typography.metadata-md}"
  focus-ring:
    backgroundColor: "{colors.focus}"
    size: 2px
  status-success:
    backgroundColor: "{colors.success}"
    rounded: "{rounded.full}"
    size: 7px
  status-warning:
    backgroundColor: "{colors.warning}"
    rounded: "{rounded.full}"
    size: 7px
  status-error:
    backgroundColor: "{colors.error}"
    rounded: "{rounded.full}"
    size: 7px
  status-running:
    backgroundColor: "{colors.running}"
    rounded: "{rounded.full}"
    size: 7px
  input:
    backgroundColor: "{colors.surface-hover}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: 8px 16px
    height: 40px
  modal:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: 22px
    width: 760px
  light-app-shell:
    backgroundColor: "{colors.light-canvas}"
    textColor: "{colors.light-text}"
  light-sidebar:
    backgroundColor: "{colors.light-sidebar}"
    textColor: "{colors.light-text-muted}"
  light-panel:
    backgroundColor: "{colors.light-panel}"
    textColor: "{colors.light-text}"
  light-selected:
    backgroundColor: "{colors.light-surface-selected}"
    textColor: "{colors.light-text}"
  light-interaction:
    backgroundColor: "{colors.light-secondary}"
    textColor: "{colors.light-text}"
  light-interaction-strong:
    backgroundColor: "{colors.light-primary}"
    textColor: "{colors.light-canvas}"
  light-interaction-soft:
    backgroundColor: "{colors.light-tertiary}"
    textColor: "{colors.light-text}"
  light-divider:
    backgroundColor: "{colors.light-line}"
    height: 1px
  light-divider-strong:
    backgroundColor: "{colors.light-line-strong}"
    height: 1px
  light-text-muted:
    textColor: "{colors.light-text-muted}"
  light-text-secondary:
    textColor: "{colors.light-text-secondary}"
  light-text-tertiary:
    textColor: "{colors.light-text-tertiary}"
  light-focus-ring:
    backgroundColor: "{colors.light-focus}"
    size: 2px
  light-status-success:
    backgroundColor: "{colors.light-success}"
    size: 7px
  light-status-warning:
    backgroundColor: "{colors.light-warning}"
    size: 7px
  light-status-error:
    backgroundColor: "{colors.light-error}"
    size: 7px
  light-status-running:
    backgroundColor: "{colors.light-running}"
    size: 7px
---

# Eidolon Design System

## Overview

Eidolon is a quiet, precise control plane for consequential local AI work. It should feel private, capable, and inspectable: closer to a well-made technical instrument than a conversational toy. The visual system is dark-first, restrained, and information-dense without becoming cramped. Warm text, graphite surfaces, exact dividers, and a desaturated blue interaction scale keep attention on state, provenance, approvals, and the user's next decision.

The interface serves a single local operator managing Project conversations, persistent Act sessions, explicit memory, reusable skills, functions, applications, schedules, approvals, agent runs, and settings. Safety boundaries must be visually legible. Trusted Eidolon chrome and sandboxed skill-owned application content must never blur into one another.

Use the current Eidolon mark in the shell. Do not invent additional brand marks, mascots, gradients, decorative illustrations, or marketing-style hero treatments. Large, faint concentric rings may sit behind page content as the single ambient motif; they must remain non-interactive and low contrast.

## Colors

Dark is the default theme. Near-black graphite layers establish hierarchy; warm off-white text avoids the glare of pure white; desaturated blue is reserved for interaction, selection, focus, running state, and high-priority actions.

- **Primary (`#5B82C4`):** active navigation indicators, reveal arrows, and strong interaction accents.
- **Secondary (`#2A3C4E`):** subdued interactive borders and secondary hover treatment.
- **Tertiary (`#BFD0EC`):** the pale blue liquid fill used by primary button transitions.
- **Canvas through surface (`#060A0E` to `#10161B`):** tightly stepped graphite layers. Prefer a divider or a small tonal change over a new container color.
- **Text (`#ECECE6`):** warm primary copy. Muted, secondary, and tertiary text progressively reduce emphasis without relying on opacity.
- **Semantic colors:** green means success or connected, amber means warning or Project mode, red means failure or destructive action, and brighter blue means running or Act mode. Always pair semantic color with a text label, icon, tooltip, or other non-color cue.

Light theme is a deliberate counterpart, not a simple inversion. It uses white canvas, warm stone sidebar and surfaces, charcoal text, and a darker blue interaction color. Components must preserve the same hierarchy and behavior when switching themes. Do not mix dark- and light-theme tokens within one surface except for isolated third-party or skill-owned application content.

## Typography

**Plus Jakarta Sans** is the interface voice. Its regular weight gives the product a calm, contemporary tone; medium weight is reserved for controls, navigation, and compact emphasis. Headings remain regular rather than bold and use slightly tight tracking.

**IBM Plex Mono** identifies machine-adjacent information: function names, code, timestamps, technical metadata, table headings, navigation group labels, and compact state details. Small mono labels may be uppercase with deliberate letter spacing. Do not set normal descriptions or long-form interface copy in monospace.

Page titles use the display token and scale responsively from `2.25rem` to `3.25rem`. Body copy uses `15px`, secondary and compact body copy use `13px`, and labels use `14px`. Body text uses `0.04em` word spacing. Preserve readable line height and avoid adding more type sizes when an existing semantic level fits.

## Layout

Desktop uses a persistent `220px` sidebar and a fluid main column. Main content uses `34px` top padding, `clamp(20px, 3.5vw, 44px)` horizontal gutters, and `60px` bottom padding; only the horizontal gutter is clamped. Content stops growing at `1280px`. Page headers put the title and descriptive copy first, actions second, and end with a divider. Never add an eyebrow line above a page title.

The spacing rhythm uses the shared steps `4px`, `8px`, `16px`, `24px`, `32px`, and `48px`. Use whitespace, dividers, aligned rows, and two-column grids before reaching for cards. Cards and bordered containment are reserved for boundaries that matter: approvals, modals, chat workspaces, and sandboxed application chrome.

At `1040px`, narrow the sidebar and collapse paired detail sections. At `800px`, convert the sidebar to a compact horizontal navigation bar. At `640px`, stack headers, grids, and the chat workspace into one column. Tables should scroll horizontally rather than destroy stable column allocation. The UI must remain usable down to `320px`.

Chat is a bounded workspace on desktop: conversation navigation and transcript scroll independently, while the composer stays anchored to the bottom. On narrow screens the workspace becomes a natural stacked document. User messages are right-aligned bubbles; Eidolon messages remain full-width transcript segments with a visible role label.

## Elevation & Depth

Eidolon is fundamentally flat. Convey most hierarchy with surface tone, 1px dividers, whitespace, and stable alignment. Do not place every section on a floating card and do not use routine drop shadows.

Use a shadow only when a layer must clearly detach from the current workflow, chiefly modal dialogs. The implemented modal shadow is broad and soft (`0 28px 90px rgb(0 0 0 / 42%)` in dark theme and `0 28px 90px rgb(20 25 32 / 20%)` in light theme) behind an `8px`-radius panel. A dark translucent backdrop reinforces that the underlying workflow is temporarily unavailable.

The ambient concentric-ring motif may create very subtle depth around the page edges. It must never compete with content, imply interactivity, or reduce text contrast.

## Shapes

The default geometry is restrained and slightly softened. Buttons, inputs, navigation rows, code blocks, and trusted application frames use a `4px` radius. Modals and approval cards use `8px` because their containment boundary is more significant. Most panels and tables remain square and divider-led.

Circular geometry is reserved for semantic dots, spinners, and the ambient ring motif. User chat bubbles are the exception to the general radius rule: use `14px 14px 4px 14px` to create a conventional right-aligned message shape. Do not introduce pill-shaped status badges; use a 7px semantic dot and plain text.

## Components

**Navigation.** Use grouped destinations with small mono group labels, simple line icons, and quiet text. Hover reveals a subtle blue-gray liquid fill. Selection adds the same surface treatment plus a 3px blue indicator; the icon and label inherit the selected text color. On compact layouts, keep the same destinations in a horizontally scrollable row.

**Buttons.** Primary buttons use `8px 16px` padding, a `32px` minimum height, medium-weight `14px` labels, and a light neutral fill. On hover, a restrained blue liquid-wave fill crosses the control; it is an interaction signature, not a general decorative effect. Secondary buttons are transparent with a strong divider border. Destructive actions use semantic red and switch to a solid red treatment on hover. Icon-only delete controls are square, carry a specific accessible name, and show a matching tooltip.

**Inputs.** Inputs use `8px 16px` padding, a `40px` minimum height, a 1px strong divider border, graphite hover surface, and `4px` radius. Hover strengthens the border; focus uses the strong interaction color plus the global visible focus ring. Labels use the `14px` compact medium-weight interface token. Textareas grow vertically and begin at `120px` minimum height.

**Lists and tables.** Prefer rows separated by 1px dividers. Table headings are `9px` uppercase mono labels; cells are compact muted text. Hover may change the row surface. Navigable skill, function, application, and schedule names reveal a small blue directional arrow on hover or keyboard focus.

**Statuses.** Render state as a 7px semantic dot with a subtle same-color halo and a plain-text label. Never encode status through color alone and never inflate passive state into a capsule. Running indicators may spin, but terminal states remain static.

**Approvals and safety boundaries.** Approval cards, permission dialogs, and trusted application chrome use explicit borders and contained surfaces because the boundary is meaningful. State what is requested, why, the risk, what approval allows, and what it does not allow. Skill-owned web applications remain inside their isolated frame with Eidolon-owned navigation and lifecycle controls visibly outside it.

**Motion.** Standard color and border transitions last `150ms`. Route entry is a subtle `180ms` fade with 4px upward travel. Reveal arrows use a short slide-and-fade; navigation selection may use the longer liquid sweep. Motion must never delay a workflow or conceal state. Under `prefers-reduced-motion`, reduce non-essential transitions and animations to effectively instantaneous behavior.

## Do's and Don'ts

- Do preserve the dark-first graphite hierarchy and the exact semantic meaning of blue, green, amber, and red.
- Do use whitespace, dividers, and aligned rows as the default structure.
- Do make safety, trust, availability, and approval boundaries explicit in both copy and layout.
- Do keep keyboard focus visible and pair every icon-only control with an accessible name.
- Do maintain WCAG AA contrast for normal text and validate both themes.
- Do preserve stable table columns and provide horizontal overflow on narrow screens.
- Do disable non-essential animation for `prefers-reduced-motion`.
- Don't wrap every section in a rounded card or add routine shadows.
- Don't use pill badges for passive status, risk, or metadata.
- Don't use color as the only indicator of mode, status, risk, or selection.
- Don't add eyebrow taglines above page titles.
- Don't use monospace for ordinary prose or introduce unneeded font weights and sizes.
- Don't blur trusted Eidolon chrome with sandboxed skill-owned content.
- Don't add gradients, glassmorphism, glossy decoration, playful illustrations, or marketing-page visual language.
