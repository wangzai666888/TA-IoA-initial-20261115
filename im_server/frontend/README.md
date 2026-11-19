# CO-IoA React frontend

The original IoA list and message layout is retained and adapted to the independent mechanism.

## Development

Start the Python API on 127.0.0.1:8000, then run:

```
npm ci --ignore-scripts --no-audit --no-fund
npm start
```

The development proxy forwards /api to port 8000.

## Validation and production

```
npm test -- --watchAll=false --runInBand
npm run build
```

The Python experiment API serves the build folder at /. Docker builds this same frontend and serves it using Nginx, forwarding /api to the API container.

The UI submits experiments, polls job status and replays completed event histories. The task timeline is explicitly an end-of-run evaluator view. Communication predictions and physical windows remain separate in the mechanism.

Model credentials are configured on the backend, never in frontend assets. See ../../README.md for model and Docker setup.
