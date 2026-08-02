# Helix frontend

React 19 + TypeScript + Vite. See the [root README](../README.md) for the whole project.

## Development

```bash
npm install
npm run dev          # http://localhost:5173, expects the API on :8000
```

Set `VITE_API_URL` to point at a different backend. **Vite inlines `VITE_*` at
build time**, so changing it requires a rebuild, not a restart.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Dev server with HMR |
| `npm run build` | Type-check then production build |
| `npm run preview` | Serve the production build locally |
| `npm test` | Vitest component and unit tests |
| `npm run test:e2e` | Playwright — starts both servers itself |
| `npm run lint` | ESLint |
| `npm run typecheck` | `tsc -b` |

## Layout

```
src/
├── api.ts               typed client; attaches the JWT, shares one refresh across concurrent 401s
├── types.ts             mirrors the backend's Pydantic schemas
├── auth/AuthContext.tsx session state
├── hooks/
│   └── useAgentTrace.ts trace WebSocket + start/finish pairing into displayable steps
├── components/
│   ├── AgentTrace.tsx   the live reasoning trace
│   ├── EscalationPanel.tsx  admin feed, reconnects with backoff
│   └── Layout.tsx       shell and navigation
└── pages/               Login, Signup, DocQA, CodeReview, SupportTriage, Observability, Billing
```

`Observability` is lazy-loaded: it pulls in the whole charting library for a page
only admins open, which would otherwise more than double the initial bundle
(265 kB with the split, 651 kB without).

## Notes

- The trace socket is opened **before** the HTTP request is sent, using a
  client-generated request id, so the trace streams from the first agent step.
- On completion each page merges the authoritative trace from the response, so a
  dropped WebSocket frame cannot leave the final steps missing.
