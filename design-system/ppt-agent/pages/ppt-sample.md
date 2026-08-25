# PPT Sample Page Override

This page inherits `../MASTER.md` and adds the following rules.

- The generated slide is the primary visual object: use one large 16:9 frame spanning the workspace width, with a compact title/count toolbar and page selector.
- Render generated HTML only inside a passive, no-permission sandbox iframe. The host UI must remain visually and behaviorally independent from sample content.
- Place the revision-feedback form immediately below the frame. Keep a visible label, concrete example, helper text, inline error, disabled/loading submission state, and a separate confirmation action.
- Page selectors remain at least 44px high, expose `aria-current`, and scroll horizontally on narrow screens without causing page-level overflow.
- Preserve frame aspect ratio at all breakpoints; controls stack below 768px. Motion stays within the global 150–300ms range and honors reduced-motion settings.
