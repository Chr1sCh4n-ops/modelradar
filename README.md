# ModelRadar Window

This repository publishes the small web version of ModelRadar.

It shows the same popover-style chart as the macOS status-bar app. It keeps only:

- the window-style page
- the rendered chart
- the data crawler
- the chart renderer
- a scheduled GitHub Pages workflow

The repository does not include the macOS app, LaunchAgents, installers, full dashboard, or private history.

## GitHub Pages

The workflow runs on pushes to `main` and on a six-minute schedule. GitHub can delay or skip scheduled runs. It fetches public model data, renders `data/chart.html`, and deploys the window page to GitHub Pages.

If a fetch fails or returns incomplete data, the deployment fails and the published page stays unchanged.

Each browser stores its own last viewed snapshot and display settings in local storage. Identical snapshots share one record with first and latest fetch times. Clearing browser data removes this history.

The page uses these public sources:

- Artificial Analysis
- OpenRouter
- models.dev
- DataLearner

The page is a generated public snapshot. It does not contain API keys or private account data.

## License

MIT. See `LICENSE`.
