# Project Setup — Question Options by Category

Ask ONE AT A TIME. Each question: 3-5 MC options with a **recommended** choice (inferred from architecture.md) + "Other" escape hatch.

## Category 1: Stack & Architecture (7 questions)

1. **Backend framework:** `FastAPI | Express | Django | Go (stdlib) | None`
2. **Frontend framework:** `React (Vite) | Next.js | React Native | Vue | None`
3. **Database:** `Postgres | MongoDB | SQLite | None`
4. **ORM/Query layer:** `SQLModel | SQLAlchemy | Prisma | Drizzle | Mongoose | None`
5. **Auth approach:** `JWT | OAuth2 | Session-based | None`
6. **API style:** `REST | GraphQL | gRPC`
7. **Project topology:** `Single product | Multi-product monorepo`

After all 7, display summary table. Confirm before proceeding.

## Category 2: Directory Structure (3 questions)

**Backend (skip if no backend):**
1. **Layout:** `Service-layer (routes → services → models → schemas) [recommended for APIs] | Domain-driven (feature folders) | Flat`

**Frontend (skip if no frontend):**
2. **Layout:** `Feature-based (features/auth/, features/tasks/) [recommended] | Product-split (products/app-a/) | Page-based (pages/ + components/)`

**Both:**
3. **Naming:** `PascalCase components + camelCase utils [recommended for JS/TS] | snake_case everywhere [recommended for Python] | Language-appropriate mix`

## Category 3: Design System — UI Projects Only (6 questions)

Skip entirely if no frontend was selected.

1. **Fonts:** `System fonts (no custom) [recommended for MVPs] | Inter + custom heading | DM Sans + Raleway | Custom (specify)`
2. **Spacing grid:** `4px base [recommended] | 8px base`
3. **Icon library:** `Lucide React [recommended for React] | Heroicons | Font Awesome | None`
4. **CSS strategy:** `Tailwind [recommended for new projects] | CSS Modules | MUI/Chakra | styled-components`
5. **Breakpoints:** `Mobile-first: 640/768/1024/1280 [recommended] | 320/768/1440 | Custom`
6. **Token location:** `src/styles/tokens.css [recommended] | tailwind.config.ts (if Tailwind) | src/theme/tokens.ts`

## Category 4: Code Conventions (6 questions)

1. **File size limit:** `Decompose at 400, hard limit 500 [recommended] | 300/400 | 200/300 (strict)`
2. **Test location:** `Alongside code (__tests__/ next to source) [recommended] | Separate tree (tests/)`
3. **Test runner:** `Vitest [recommended for Vite/TS] | Jest [recommended for RN] | Pytest [recommended for Python]`
4. **Formatter:** `Prettier [recommended for JS/TS] | Ruff [recommended for Python] | Biome`
5. **Linter:** `ESLint [recommended for JS/TS] | Ruff [recommended for Python] | Biome`
6. **Controller thickness:** `Thin (routes delegate to services) [recommended] | Fat (logic in handlers)`
