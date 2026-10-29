---
title: Neermattam
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
short_description: Flood depth from one street photo, live map and routes
---

# Neermattam

Neermattam (Tamil: water level) measures how deep floodwater is on a street from one photo, fuses many reports into a live depth map of Chennai, and routes each kind of vehicle around water it cannot cross. Every route is advisory.

This Space runs all three services in one container:

- the gateway (Go), which serves the pages on port 7860;
- the engine (Rust), which fuses readings into the hex map and plans routes;
- perception (Python), which reads the photo with Muse Spark Open (served by Baseten) and segments wheels and water with SAM 2.1 small, run locally on the CPU.

The map runs with a labelled simulated scenario based on places that flooded during Cyclone Michaung in December 2023. The depths in it are invented, and the map says so. Real reports are fused next to it and outlined.

To protect the shared model credit, each visitor may send a few reports a minute, and the whole Space makes at most a fixed number of model calls a day. Beyond that, measurements pause until midnight India time while the map and routes keep working.

Road data: the six city road extracts in `data/osm/` are © OpenStreetMap contributors, available under the Open Database Licence. See `DATA_LICENSE`.

How this Space is built and updated: `docs/DEPLOY.md` in the source repository.
