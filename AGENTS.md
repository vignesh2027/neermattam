# Neermattam build rules

Neermattam (Tamil: water level) measures how deep and how fast floodwater is on a street from one photo or short video, fuses many reports into a live depth map of Chennai, and routes each kind of vehicle around water it cannot cross. Built for the Meta Global AI Developer Hackathon (Oct 26 to Nov 4, 2026). Everything in this repository was written during the hackathon.

## Hard rules
1. **Languages:** Go, Rust and Python only. Browser pages are server-rendered HTML and CSS templates. The only browser script allowed is a vendored copy of MapLibre GL JS plus the smallest possible inline glue. No TypeScript, no Node toolchain, no frontend framework.
2. **Models:** Meta models only, no other provider's model anywhere.
   - **Muse Spark Open** (Meta open weights, served by Baseten) is the one reasoning model: scene reading, the second waterline, mask verification and voice reports.
   - **SAM 2.1** (Meta open weights, on Modal or locally) is used only for masks and tracking. SAM 3.1 replaces it by configuration once its gated weights are available to us.
   - The hosted Meta Model API path (Muse Spark 1.3, SAM 3.1, Muse Voice Transcribe) is implemented behind the same interfaces and switches on when that account can make calls.
3. **Honest numbers.** Every accuracy, latency or cost figure in docs or the UI comes from a script in `eval/` that anyone can rerun. Never invent a number.
4. **Safety.**
   - A route is always labelled advisory.
   - A cell with too little evidence shows "unknown", never "safe".
   - Uncertainty is carried everywhere a depth is shown.
5. **Secrets** live only in `.env`, which is gitignored. Never print or commit keys.
6. **Writing:** human, plain, professional English. No em dashes and no arrows in prose or UI text.
7. **Authorship:** do not commit. The maintainer reviews and commits. No co-author lines.
8. **Tests:** every module ships with tests, and the build stays green: `cargo test`, `go test ./...`, `pytest` (perception and mcp).

## Layout
```
engine/       Rust: hex-grid fusion with time decay, hazard rating, OSM road graph, vehicle-aware routing, HTTP API
perception/   Python: geometry (wheel ellipse, waterline, depth with uncertainty), velocity from tracks, model adapters (Meta Model API, open weights), HTTP service
gateway/      Go: web pages, upload, WhatsApp webhook, job orchestration, SSE live map, rate limits
eval/         Python: ground-truth harness, published results
mcp/          Python: MCP server over the gateway's API, for AI agents
docs/         Architecture, results, decisions
data/         Gitignored local data (OSM extract, uploads)
```

## Contracts between services
See `docs/CONTRACTS.md`. Change a contract only by editing that file first and updating every side in the same change.

## Visual identity
- Light blue premium theme. Background #F4F9FF, surface #FFFFFF, primary #2563EB, soft blue #DBEAFE, text #0B1B33.
- Accent: deep violet #4C1D95.
- Danger states: deep red #9F1239.
- Depth scale from light to deep: #BAE6FD, #38BDF8, #2563EB, #4C1D95, #9F1239.
- Unknown cells are grey hatching.
