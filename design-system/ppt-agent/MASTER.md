# PPT Agent Design System

PPT Agent deliberately shares the established Image Agent Studio visual language. This file records the implemented tokens so the repository has one source of truth.

## Foundations

- Product pattern: desktop-first productivity workspace with a sticky top navigation, project sidebar, stage rail, and one primary decision area.
- Heading font: Space Grotesk; body font: DM Sans with system CJK fallbacks.
- Primary: `#6d28d9`; strong primary: `#5b21b6`; soft primary: `#f3e8ff`.
- Background: `#f7f5fb`; surface: `#ffffff`; text: `#172033`; secondary text: `#5d667a`; border: `#e7e2ef`.
- Spacing follows a 4/8px rhythm. Interactive targets are at least 44px high.
- Component corners use 8px, 12px, and 18px tiers. Shadows remain subtle and functional.

## Interaction rules

- Use the existing outline SVG family; never use emoji as structural icons.
- Show `focus-visible` rings and nearby validation errors.
- Disable actions during loading and name the operation in progress.
- Keep transitions between 150–300ms and respect `prefers-reduced-motion`.
- Markdown is escaped before the renderer adds the supported structural tags.
- At narrow widths, navigation wraps, the project list scrolls horizontally, the stage rail becomes horizontal, and content remains single-column without page-level horizontal overflow.
