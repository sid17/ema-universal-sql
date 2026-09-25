# Project Type Modulation

Different project types can skip certain phases:

| Type | Skip |
|------|------|
| Backend API | Design review |
| CLI tool | Design review, platform deploy (manual release) |
| Full-stack web | Nothing skipped |
| Mobile (React Native) | Platform deploy (app store is manual) |

Use this table when routing phases — if a phase is skippable for the project type, mention it to the user but don't enforce it.
