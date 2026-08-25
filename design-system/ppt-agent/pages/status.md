# Agent Status Page Override

This page inherits `../MASTER.md` and adds the following rules.

- Lead with four compact operational metrics: active task, stage and elapsed time, model, and event/error totals.
- Use a small event-density strip to reveal bursts and gaps without introducing a charting dependency. Every mark has a text tooltip and the detailed list remains the authoritative view.
- Keep the latest failures in a dedicated, always-visible recovery area above filters.
- Merge Job, model, Skill read, validation, artifact, project, and error events into one list. Provide type filters, local search, newest/chronological ordering, expandable redacted JSON, and copy feedback.
- Search no-results must offer a reset action. Errors use text labels as well as color. All filter, expand, sort, and copy targets remain keyboard operable and at least 44px tall.
- Use two-column metrics at tablet widths and one column below 768px. Event timestamps wrap below summaries on narrow screens; no horizontal page scroll.
