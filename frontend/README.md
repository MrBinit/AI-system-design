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

## Local Dev API Wiring

- The Vite dev server proxies `/api` and `/healthz` to `http://localhost:8000`.
- If you need another backend URL, set `VITE_API_BASE` in `frontend/.env`.

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
