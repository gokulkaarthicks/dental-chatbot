---
kind: frontend_style
name: CSS Design Tokens, BEM-style Component Styles, and Scoped Staff Dashboard
category: frontend_style
scope:
    - '**'
source_files:
    - frontend/src/styles/global.css
    - frontend/src/styles/chat.css
    - frontend/src/styles/cards.css
    - frontend/src/pages/staff/staff.css
    - frontend/package.json
---

## Approach

The frontend is a Vite + React app styled with plain CSS (no Tailwind, no CSS-in-JS, no component library). Visual consistency is achieved through a single shared design-token file plus per-feature CSS modules. The styling methodology is **BEM-like** (`.block__element--modifier`) applied to hand-written CSS files under `frontend/src/styles/`.

## Key Files

- `frontend/src/styles/global.css` — the canonical token surface: color ramp (`--c-0`..`--c-900`), accent (`--accent`, `--clay`), status triplets (`--ok-*`, `--warn-*`, `--danger-*`), semantic aliases (`--bg`, `--surface`, `--border`, `--text`), radii (`--r-sm/md/lg`), spacing scale (`--s-1`..`--s-16`), fonts (`--font`, `--display`, `--mono`), and global base styles for buttons, inputs, cards, pills, stats, skeletons, and legacy overview layout.
- `frontend/src/styles/chat.css` — patient chat thread layout (sidebar + header + scrollable thread + sticky composer) using the shared tokens; defines `.chat`, `.practice`, `.msg`, `.composer`, `.prompts`, and responsive breakpoints at 980px / 640px.
- `frontend/src/styles/cards.css` — interactive card variants rendered inside messages (slots, proposals, confirmations, family, handoff); class prefix `cardx` to distinguish from the legacy `.card` in `global.css`.
- `frontend/src/pages/staff/staff.css` — staff dashboard scoped under the `sd-` namespace so it cannot leak into the patient chat; documents its own layout contract (232px fixed rail, fluid main column, 1200px measure, white panels with hairline borders).

## Architecture and Conventions

1. **Single source of truth for tokens.** All colors, spacing, radii, and typography are consumed via CSS custom properties on `:root` in `global.css`. Feature sheets never hard-code hex values for theme colors; they reference `var(--accent)`, `var(--ok-bg)`, etc. This is documented explicitly in the top comment of `staff.css`: "Built on the tokens in global.css: one neutral ramp, one accent, 4px spacing scale, three radii."

2. **Feature-scoped namespaces.**
   - Patient chat uses unscoped names like `.chat`, `.msg`, `.composer`.
   - Interactive cards use the `cardx` prefix to avoid colliding with the legacy `.card` token defined in `global.css`.
   - Staff dashboard uses the `sd-` prefix throughout (`sd-shell`, `sd-sidebar`, `sd-panel`, `sd-list`, `sd-table`, `sd-badge`, `sd-drawer`, `sd-modal`, …) so the two UI surfaces remain visually isolated.

3. **BEM-like naming.** Elements and modifiers follow `.block__element` and `.block--variant` patterns consistently across all three style sheets (e.g. `.chat__header`, `.msg--patient`, `.cardx--slots`, `.sd-stat__value`, `.sd-badge--ok`).

4. **Design language rules (documented in comments):**
   - Palette: "a single neutral (slate) ramp plus one accent (deep teal) for primary calls-to-action, plus three status hues used only where state is communicated" (`global.css` header comment).
   - Status semantics: ok/warn/danger are used for state, not decoration.
   - Spacing: explicit 4px base scale (`--s-1 = 4px`).
   - Radii: 3-step scale (`--r-sm: 8px`, `--r-md: 12px`, `--r-lg: 16px`).
   - Cards: "white panel with a single hairline border — no shadows on static surfaces" (`staff.css` header comment).
   - Chat visual language: "light canvas with the conversation raised off it. Tinted surface steps rather than shadows; one accent used for the send control and the patient's own turns."

5. **Responsive strategy:** Media queries are written inline in each feature sheet rather than via a breakpoint system. Common thresholds observed: 720px (legacy overview), 640px (chat/mobile), 980px (hide practice sidebar), and 1200px max-width for the staff main column.

6. **Accessibility signals present but not enforced by tooling:** `prefers-reduced-motion` media queries disable animations in both `global.css` and `chat.css`; `.sr-only` utility exists in `chat.css`; focus-visible outlines are set globally.

7. **No external styling framework.** `package.json` lists only `react`, `react-dom`, and `lucide-react` as runtime dependencies; there is no Tailwind config, no PostCSS plugin, no CSS preprocessor dependency. Build is `vite build` with TypeScript typecheck.

## Conventions and Constraints

- **Observed conventions (descriptive):**
  - New colors go into `:root` variables in `global.css`, not as literal hex values in components.
  - New UI surfaces get their own CSS file under `src/styles/` and scope their selectors under a unique namespace (`sd-` for dashboards, `cardx` for interactive cards).
  - Class names follow BEM-like block/__element--modifier form.
  - Spacing and radii come from `--s-*` and `--r-*` variables.
  - Status colors are applied via the `--ok-*` / `--warn-*` / `--danger-*` triplets.
  - Animations respect `prefers-reduced-motion`.

- **Enforced constraints:**
  - There is no linter or schema enforcing these conventions; enforcement is manual/code-review based.
  - The only build-time constraint relevant to styling is that the project runs `tsc --noEmit && vite build`, which does not validate CSS.
  - The `sd-` namespace in `staff.css` is a convention, not a CSS scoping mechanism — it relies on authors choosing the prefix.