# Firstsource Brand Quick Reference

Condensed brand standards for building dashboards and HTML output. This is the authoritative visual reference for this skill.

## Company
- **Name**: Firstsource (part of RP-Sanjiv Goenka Group)
- **Tagline**: "We make it happen!"
- **Personality**: Confident, energized, practical, precise

## Color Palette

| Role | Name | Hex | Notes |
|---|---|---|---|
| Primary accent | Firstsource Orange | `#DF6014` | Highlights, active states, CTAs. Use sparingly. NEVER in gradients. |
| Primary dark | Dark Blue | `#1E2247` | Headings, headers, table header rows, gradient base |
| Secondary | Mid Blue | `#113190` | Borders, dividers, gradients, accent badges |
| Secondary | Bright Blue | `#2844C4` | Gradients, page-number/accent chips |
| Secondary | Light Blue | `#6CB1DB` | Dividers, accent rules, badges. NEVER a gradient background. |
| Neutral | Gray | `#ECF1F5` | Solid section/background fills only |
| Neutral | White | `#FFFFFF` | Text on dark, solid backgrounds. NEVER a gradient background. |
| Neutral | Black | `#000000` | Body text on light backgrounds only |

**Gradients**: subtle linear diagonal, dark → light, left → right, starting ~1/3 in. Use ONLY Dark Blue / Mid Blue / Bright Blue. Never Orange, Light Blue, White, Black, or Gray. Full-bleed backgrounds only.

**Data-viz series order**: Dark Blue → Mid Blue → Bright Blue → Orange → Light Blue.

## Typography
- Brand font: **Neue Haas Grotesk Display** (bundled in `assets/fonts/`).
- HTML/CSS stack:
  ```css
  font-family: 'Neue Haas Grotesk Display', 'Franklin Gothic Medium', Arial, sans-serif;
  ```
- Headings: Dark Blue `#1E2247`, Bold/Black weight.
- Body: Black on light, White on dark. 11–12pt equivalent.
- Never all-caps or italics for extended text (small stylistic use only).

### @font-face block for self-contained HTML
Embed weights actually used (Roman, Medium, Bold, Black recommended):
```css
@font-face { font-family:'Neue Haas Grotesk Display'; font-weight:400; src:url('assets/fonts/NeueHaasDisplayRoman.ttf'); }
@font-face { font-family:'Neue Haas Grotesk Display'; font-weight:500; src:url('assets/fonts/NeueHaasDisplayMediu.ttf'); }
@font-face { font-family:'Neue Haas Grotesk Display'; font-weight:700; src:url('assets/fonts/NeueHaasDisplayBold.ttf'); }
@font-face { font-family:'Neue Haas Grotesk Display'; font-weight:900; src:url('assets/fonts/NeueHaasDisplayBlack.ttf'); }
```
Note the Medium filename is `NeueHaasDisplayMediu.ttf` (no "m"). For a fully portable single file, base64-embed the fonts; otherwise keep the `assets/fonts/` folder alongside the HTML.

## Logos (in `assets/logos/`)
- `Firstsource-logo-standalone.png` — dark wordmark, for LIGHT backgrounds.
- `Firstsource-logo-white.png` — white wordmark, for DARK backgrounds / blue gradients.
- `RPSG-Group-logo-standalone.png` — parent group, optional on title/closing areas.

Rules: Firstsource is the dominant logo. On dark blue backgrounds use the white version. Maintain clear space; never stretch, distort, or recolor.

## Layout
- Text over images/photos needs a semi-transparent Dark Blue overlay.
- Holding shapes: solid brand colors, consistent height when side-by-side, ~20px gaps.
- Accent rule: thin Light Blue `#6CB1DB` horizontal line under section headings.

## Footer
`Copyright © {CURRENT_YEAR} Firstsource. All rights reserved.` (replace year at build time). The `© FIRSTSOURCE | CONFIDENTIAL` format is deprecated.

## Messaging pillars
- **Problem Solving** — intimate customer knowledge + intelligent automation.
- **Iteration** — a bias for doing, powered by smarter AI/ML.
- **Hyper-focus** — experts + the right data, thinking deeply about your business.
