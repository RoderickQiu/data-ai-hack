# the right job — 4-minute pitch

The current deck has exactly **five slides**, uses a cyan theme, and includes the demo within four minutes.

- [Editable PowerPoint](output/the-right-job-pitch.pptx)
- [PDF](output/the-right-job-pitch.pdf)
- [Speaker script](speaker-script.md)

| Slide | Content | Time |
| --- | --- | --- |
| 1 | Job-search pain points | 0:00–0:35 |
| 2 | the right job — product name | 0:35–0:50 |
| 3 | Product value | 0:50–1:35 |
| 4 | Sponsor implementation: Hotdata, Cognee, HydraDB, RocketRide, Rote; Snyk checks | 1:35–2:30 |
| 5 | Demo time | 2:30–4:00 |

The older `second-nature-pitch.*` files are previous versions; use the files in `output/` for this presentation.

## Dashboard

Open `../dashboard/index.html` to preview the cyan interface. React is bundled locally and JSX is precompiled, so rendering does not need a CDN. Keep the `vendor/` folder alongside the HTML.

For actual run data, run `make dashboard-serve` from the repository root and open http://127.0.0.1:8080/. Without `data.json`, the dashboard explicitly displays example data. The demo script distinguishes example charts from measured results.
