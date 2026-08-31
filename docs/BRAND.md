# AgentSeat brand

AgentSeat's mark is the collaboration head held by a thin open arc. The head is
the Agent; the arc is its bounded seat inside one application. The gap is
intentional: AgentSeat participates in the user's desktop without enclosing or
replacing it.

## Assets

- `assets/brand/agentseat-mark.svg` — primary transparent mark;
- `assets/brand/agentseat-mark-mono.svg` — one-color reproduction;
- `assets/brand/agentseat-hero.svg` — repository and documentation hero;
- `assets/brand/agentseat-social-preview.svg` — 1280×640 social artwork;
- `assets/brand/agentseat-mark-{64,128,256,512}.png` — raster exports;
- `assets/brand/agentseat-social-preview.png` — social artwork raster export.

The SVG mark is the source of truth. Use a supplied raster size rather than
resizing a screenshot or the 52 px runtime collaboration head.

Regenerate all PNG exports with `./scripts/render-brand-assets.sh`. It uses
`rsvg-convert`, so every committed raster is derived from the vector master.

## Palette

| Token | Value | Use |
| --- | --- | --- |
| Ink | `#080C11` | Head and dark surfaces |
| Ice | `#A9DCF7` | Boundary arc and restrained accents |
| Snow | `#FFFFFF` | Eyes and primary text on dark surfaces |
| Slate | `#B8C3CE` | Secondary text on dark surfaces |

Keep the mark flat and high contrast. Do not thicken the arc, add a mouth or
eyebrows, rotate the eyes into an angry expression, or place effects inside the
transparent mark. Give the mark clear space equal to at least one eye width.

These brand assets are distributed under the repository's MIT License.
