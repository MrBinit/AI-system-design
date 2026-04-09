# Frontend (React + Tailwind)

This frontend is built with:

- React
- TypeScript
- Vite
- Tailwind CSS

## Scripts

- `npm install`
- `npm run dev`
- `npm run build`
- `npm run preview`

## Environment Setup

1. Local development:
   - copy `frontend/.env.development.example` to `frontend/.env.development`
   - set `VITE_DEV_API_TARGET` if backend is not `http://localhost:8000`
2. Production build:
   - default is relative API (`/api/...`) for CloudFront path routing
   - only set `VITE_API_BASE` in `.env.production` if using a separate API domain

## Local Dev API Wiring

- Vite dev server proxies `/api` and `/healthz` to `VITE_DEV_API_TARGET` (default: `http://localhost:8000`).
- Frontend API calls in production use relative URLs by default, so CloudFront can route `/api/*` to backend origin.

## Login

The app logs in via:

- `POST /api/v1/auth/login`

Default credentials:

- `admin`
- `admin`

## API Endpoints Used

- `POST /api/v1/auth/login`
- `POST /api/v1/chat/stream`
- `GET /api/v1/eval/conversations`

## Production Serving

FastAPI serves `frontend/dist` at `/ui` after build.

## One-Command Frontend Deploy

From repo root:

- `./scripts/frontend-deploy.sh`

Optional overrides:

- `./scripts/frontend-deploy.sh --bucket <s3-bucket> --distribution <cloudfront-id> --region <aws-region>`
