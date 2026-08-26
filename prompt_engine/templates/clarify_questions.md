# Clarification questions

Identify only missing facts that materially change the presentation narrative. Return concise questions with useful choices, one recommended choice, and free-text support.

This is an automatic multi-round clarification flow. Use the task and every prior
question/answer pair as accumulated context. Never repeat an answered field or ask
for information that is already known. When no additional high-value clarification
is needed, return `{"needs_clarification": false, "questions": []}`. Otherwise return
`{"needs_clarification": true, "questions": [...]}`.
