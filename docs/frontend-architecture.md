# Frontend Architecture

Stage 10E establishes a product foundation without changing the chat or
knowledge contracts. The frontend remains static HTML, CSS, and ES modules;
there is no build system to maintain.

## Application shell

`web/index.html` is the stable shell:

- `Sidebar` owns global navigation, new conversation, search, history, and user/settings entry points.
- `WorkspaceHeader` owns execution context: Chat/Agent, model or agent, and the configuration inspector.
- `WorkspaceContent` owns the conversation, welcome state, messages, thinking, sources, citations, and message actions.
- `ComposerRegion` owns prompt input, attachments, notebook/context selection, optional tools, and send state.
- `surface-backdrop` hosts secondary workflows such as Settings, Lab, Agents, Notebooks, and run diagnostics.

Every new feature must first choose one of these regions. Conversation actions
must not become global sidebar navigation.

## Tokens and geometry

`web/assets/tokens.css` is the semantic source of truth. It defines colors,
spacing, radii, control heights, sidebar width, content width, composer width,
focus-safe contrast, and overlay/shadow values. `app.css` contains the existing
feature styling; `shell.css` contains the Stage 10E shell and reusable
primitives. The compatibility aliases in `tokens.css` let existing rendering
rules migrate incrementally without changing behavior.

Use the shared geometry first:

- conversation: `--content-max-width`;
- composer: `--composer-max-width`;
- standard spacing: `--space-1` through `--space-8`;
- controls: `--control-height-sm`, `--control-height-md`, `--control-height-lg`;
- surfaces: `--color-bg-surface`, `--color-bg-elevated`, `--color-border`.

Do not add a pixel correction to align one feature. If a new surface needs a
new size, add a semantic token only when the value is shared and justified.

## Components and actions

Use the primitives in `shell.css`: `.button`, `.primary-button`,
`.text-button`, `.icon-button`, `.panel`, `.divider`, `.empty-state`,
`.status-indicator`, and `.spinner`. Existing domain primitives remain named
by their responsibility: `.message`, `.message-toolbar`, `.thinking-block`,
`.message-sources`, `.composer`, `.agent-picker`, `.notebook-picker`, and
`.config-surface`.

- Primary: send, save, confirm.
- Secondary: configuration, branching, navigation.
- Utility: copy, details, scroll-to-latest. Use `.icon-button`, `title`, and an accessible label.
- Destructive: delete, remove, reset. Use the danger token and confirmation where data is affected.

Technical details are progressively disclosed with `details`/`summary` or a
surface. Normal answers should foreground content, responding/thinking status,
sources, and compact actions; retrieval, reranking, grounding, and timings
belong under details or diagnostics.

## Themes and responsive behavior

Light, Dark, and System set `data-color-scheme` and resolve through the same
semantic tokens. Components must consume tokens rather than assume a white or
black surface. `data-ui="developer"` changes density and typography without
changing the shell hierarchy.

At `700px` the sidebar becomes a drawer with the existing backdrop. The
workspace keeps the same header/content/composer order, secondary controls wrap
or hide, and the composer remains usable. At `420px` gutters use the smallest
shared spacing and composer controls are allowed to wrap instead of creating
horizontal scrolling.

## Adding a new frontend feature

1. Classify it as sidebar navigation, header context, conversation content, composer capability, or secondary surface.
2. Reuse an existing primitive and token before adding CSS.
3. Keep API and runtime semantics out of presentation-only changes.
4. Use progressive disclosure for diagnostics and infrequent actions.
5. Provide an accessible name, visible focus state, and keyboard path.
6. Check Light, Dark, System, narrow mobile, long content, and empty/error states.
7. Add a focused regression for any new shell invariant.
