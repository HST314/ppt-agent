# Acceptance Page Override

This page inherits `../MASTER.md` and the preview/history hierarchy from
`ppt-full.md`.

- Treat the approved full-deck revision as a read-only acceptance baseline.
  Show its textual status, revision number, page count, revision hash, and package
  hash before the preview.
- Keep the safe 16:9 preview and immutable revision history available. Audit JSON
  and HTML-PPT ZIP downloads remain explicit labeled links rather than icon-only
  controls.
- A follow-up feedback form may create a child revision, but must explain that the
  approved baseline stays unchanged and that the workflow returns to the full-deck
  workspace.
- The approval action has no confirmation dialog. Its button names the transition,
  exposes a loading state, reports an inline error, and moves focus to the
  acceptance `h2` after success.
- At widths below 768px, the acceptance evidence grid becomes one column and all
  transition/download actions fill the available width without horizontal scroll.
