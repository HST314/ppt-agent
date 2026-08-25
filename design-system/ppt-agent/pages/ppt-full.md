# PPT Full-Deck Page Override

This page inherits `../MASTER.md` and the interaction hierarchy established by
`ppt-sample.md`.

- Keep the 16:9 HTML-PPT preview as the primary visual object. Before a complete
  package exists, use the same frame to show the approved sample source package
  and place the ordered page-slot summary immediately below it.
- Show every slot with page number, title, textual readiness, and textual source.
  Color may reinforce `ready` or `pending`, but must never carry status alone.
- Place full-deck feedback directly below preview and plan content, then generation
  attempts, then immutable revision history. This matches the sample workspace's
  reading and action order.
- Revision selectors remain native buttons with `aria-pressed`, visible
  `focus-visible` treatment, and at least 44px targets. Async selection restores
  logical focus to the selected revision without moving the page unexpectedly.
- Entering from the sample is a single action without a confirmation dialog. The
  button names its loading state, errors appear next to it and in the toast region,
  and success focuses the full-deck h2 while the live region announces the change.
- At widths below 768px, toolbars and actions stack, page slots use one column,
  the preview retains 16:9, and no component introduces page-level horizontal
  scrolling. Motion uses the global 150–300ms rhythm and reduced-motion override.
- Full-deck package documents use the same script-only sandbox and network-denying
  response policy as sample packages; package markup is never inserted into the
  parent document.
