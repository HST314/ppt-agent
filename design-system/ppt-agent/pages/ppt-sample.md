# PPT Sample Page Override

This page inherits `../MASTER.md` and adds the following rules.

- The generated slide is the primary visual object: use one large 16:9 frame spanning the workspace width, with a compact title/count toolbar and page selector.
- Render the package entry only inside a script-enabled sandbox iframe without same-origin, form, popup, download, or navigation permissions. Apply a response CSP that permits package-local scripts and assets while blocking network access. The host UI must remain visually and behaviorally independent from sample content.
- Place the revision-feedback form immediately below the frame. Keep a visible label, concrete example, helper text, inline error, disabled/loading submission state, and a separate confirmation action.
- Place the revision-history card after the feedback form. Each revision has a keyboard-operable selector with a clear selected state; choosing a revision moves the current pointer without creating content history, and the next AI edit becomes its child.
- A tool-round-limit failure exposes an adjacent recovery control: the primary action continues with 10 rounds, a labeled select offers 5/10/20, helper text explains checkpoint reuse and the 100-round chain cap, and stale checkpoints explain why continuation is disabled.
- The HTML-PPT owns page navigation inside the preview. Host controls expose package metadata and whole-package export, with at least 44px targets and no page-level horizontal overflow.
- Preserve frame aspect ratio at all breakpoints; controls stack below 768px. Motion stays within the global 150–300ms range and honors reduced-motion settings.
