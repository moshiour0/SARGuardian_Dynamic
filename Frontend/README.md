# SARGuardian — standalone frontend

React + TypeScript + Vite frontend for SARGuardian's AOI-driven Earth-observation workflows.
The RiverShift workspace provides a global, location-independent NISAR L-band GCOV screening path for
eligible river reaches: users draw a WGS84 AOI, choose a date window, and the application discovers
compatible acquisitions for that selection. The Jamuna–Chowhali run is a demonstration case, not a fixed
geographical target. The RiverShift card is labelled RESEARCH PREVIEW because each result's scientific
review state is specific to its AOI and observation window.

## Run locally

Install Node.js 22 or newer, then open a terminal in this folder:

```sh
npm ci
npm run dev
```

Open http://localhost:5173. The home page loads without configuration.

For Google login and the map, copy `.env.example` to `.env.local` and fill in
your own Firebase and Google Maps project values. Set `VITE_SARGUARDIAN_API_URL`
to the deployed metadata API URL (or leave `http://localhost:8000` for local
development). All variables in this file are public browser configuration:
`VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_APP_ID`, `VITE_FIREBASE_AUTH_DOMAIN`,
`VITE_FIREBASE_MEASUREMENT_ID`, `VITE_FIREBASE_MESSAGING_SENDER_ID`,
`VITE_FIREBASE_PROJECT_ID`, `VITE_FIREBASE_STORAGE_BUCKET`,
`VITE_GOOGLE_MAPS_API_KEY`, and `VITE_SARGUARDIAN_API_URL`. Never put Supabase
server keys, Firebase Admin JSON, NASA Earthdata passwords, or tokens in any `VITE_*` variable.
Restart after changing them. The original
account configuration is not bundled in this standalone copy.

The Landslide page keeps the original Google sign-in requirement. Configure
Google sign-in and the authorized domain in your Firebase project. External
map, image, and search services require an internet connection. The map still
requires a Google Maps API key; check that provider's current billing and quota
conditions before publishing a public demo.

## Run with the metadata API

Start the backend from `SARGuardian_Dynamic/Backend` (see its README), then
set `VITE_SARGUARDIAN_API_URL=http://localhost:8000`. The lightweight archive
metadata endpoint searches ASF and returns candidate granules plus reported
full-file sizes. That endpoint alone does not download or process satellite data.

**RiverShift uses a separate processing-job path.** Its authenticated job API
validates the user-supplied AOI/date request, discovers compatible NISAR GCOV
acquisitions, and dispatches the RiverShift worker in
[`moshiour0/SARGuardian-backend`](https://github.com/moshiour0/SARGuardian-backend/tree/main/tools/river_shift).
The worker downloads and verifies actual GCOV products, performs the documented
water/land-transition screening, and returns source-linked outputs. The live
worker requires at least four distinct acquisition dates, at least 90% catalogue
footprint coverage per selected date, and compatible orbit/frame, product and
raster support. The request envelope is one single-ring WGS84 Polygon up to
1,000 km² with a maximum 366-day observation window. These are data/processing
eligibility gates, not country restrictions.

## Build

```sh
npm run build
npm run preview
```

Production output is generated in `dist/`. Preview runs at http://localhost:4173.
For Vercel, set the project root to `Frontend/`, use the Vite preset, build command
`npm run build`, and output directory `dist`. Set the public frontend environment variables
before building. The backend is a separate Vercel project rooted at `Backend/`; set its
server-side variables in that project only. The included SPA rewrite supports opening `/landslide` directly.

## Included files:

- `src/`: home page, navigation, workflow cards, styles and Firebase configuration.
- `src/pages/RiverShiftPage.tsx`: global AOI-driven NISAR GCOV discovery, authenticated job submission, actual-date results, change layers and optional exposure context.
- `landslide/ui/`: Landslide investigation screen and its styles.
- `landslide/engine/`: browser-side types, validation, and fixture adapter
  required by that screen.
- `landslide/fixtures/` and the existing TypeScript tests.
- Vite, TypeScript, ESLint, package manifest, and dependency lockfile.

The two actions described below are specific to the **Landslide workspace**:
`Check NISAR archive` calls the lightweight metadata preflight API, while
`Reference demo` uses the Landslide page's precomputed fixture and is not live
analysis. This fixture status does not describe RiverShift. RiverShift is a
separate, AOI-driven NISAR GCOV processing workflow connected to the authenticated
job API and backend GitHub Actions worker; its outputs remain research-screening
candidates until independently reviewed for the selected region and dates.

## বাংলা নির্দেশনা:

এটি আগের frontend-এর আলাদা কপি। ZIP extract করে এই ফোল্ডারে terminal খুলুন,
তারপর `npm ci` এবং `npm run dev` চালান। Browser-এ `http://localhost:5173`
খুললে Home page পাবেন। Google login ও map চালাতে `.env.example` দেখে
নিজের configuration দিয়ে `.env.local` তৈরি করতে হবে। Landslide-এর `Reference demo` ফলাফল fixture-ভিত্তিক; এটি RiverShift থেকে আলাদা।
RiverShift authenticated job workflow দিয়ে actual NISAR GCOV data process করে।
