# Neermattam engine

The engine is the Rust service at `engine/` (crate `neermattam-engine`, port 8302). It does four things:

1. Fuses street-level flood depth readings into a live hex map, with uncertainty that grows with time. It covers six metros by default: Chennai, Mumbai, Bengaluru, Hyderabad, Kolkata and Delhi.
2. Rates each cell with the UK Defra and Environment Agency flood hazard formula.
3. Loads each city's road network from OpenStreetMap into a compact directed graph.
4. Routes pedestrians, two-wheelers, autos, cars and buses around water each of them should not cross, and returns the plain shortest route next to it for comparison.
5. Gives city control rooms ranked hotspots, a summary, each cell's depth history with a drain rate, and an export for their own tools.
6. Watches critical sites (data centres first, then hospitals and telephone exchanges): the water in rings around each site, access routes for a diesel tanker, a two-wheeler and a car, and a long-lived risk record per area for siting. See [Critical sites](#critical-sites) and docs/SITES.md.

Every route is advisory. A cell with too little evidence is reported as `unknown`, never as safe, and every depth the engine returns comes with its standard deviation. The rules that keep a flooded road from being shown as safe are collected in [Safety model](#safety-model).

## Why it is built this way

Reports from the public are noisy, arrive out of order, repeat each other, contradict each other and go stale. A per-cell Kalman filter handles this with a few lines of arithmetic: each report is weighted by how certain it is, old information grows less certain without being assumed to drain away, reports that contradict each other widen the estimate instead of averaging it, and late reports are folded in as if they had arrived on time. An H3 hex grid gives every cell the same shape and six equidistant neighbours, so a report can inform the street next to it in a principled way. Routing then only needs, for each road segment, the list of cells it touches, which is computed once when the graph is loaded.

## Fusion model

Each H3 cell (resolution 10 by default, edges of about 66 m) holds two independent Gaussian states: depth in centimetres and, once anyone has measured it, surface velocity in metres per second.

**Holding the level.** Between two moments `dt` hours apart:

```
mu  = mu                         the last level is held
P   = P + q^2 * dt               uncertainty grows like a random walk
```

`q` is 6 cm per square-root hour by default: after one hour without news a cell gains 6 cm of standard deviation, after four hours 12 cm. Velocity uses the same steps with `q = 0.2 m/s` per square-root hour. The engine does not assume water recedes: Chennai streets can stay flooded for days, and an assumed recession is exactly what turns an old report of deep water into a green light. Recession is an opt-in (`ENGINE_HALF_LIFE_HOURS`), in which case `a = 2^(-dt / half_life)`, `mu = a * mu` and `P = a^2 * P + q^2 * dt`.

**Update.** A reading `z` with standard deviation `s` and trust `w` in (0, 1] is fused by inverse-variance weighting, with observation variance `R = s^2 / w`:

```
mu = (mu / P + z / R) / (1 / P + 1 / R)
P  = 1 / (1 / P + 1 / R)
```

The first reading in a cell becomes its state. Trust is computed server-side by the gateway from the source and the checks it ran; the engine never takes it from the person reporting.

**Conflicting readings.** When a direct reading differs from the cell's direct estimate by more than `3 x sqrt(sigma_old^2 + sigma_new^2)`, the two cannot both be right, and averaging them would give a confident answer that matches neither. With `delta` the difference:

- a newer reading replaces the mean, with variance `max(sigma_new^2, delta^2 / 4)`;
- an older reading is fused with its variance inflated to at least `delta^2 / 4`.

Either way the cell counts a conflict (`conflicts`), `delta^2 / 4` becomes a floor on the cell's direct variance, and the deeper of the two disagreeing means is kept as `conflict_high_mean_cm`, which routing blocks on. One 40 cm report followed by ten dry ones within twenty minutes shows 0 cm with a sigma of 20 cm, one conflict and a conflict mean of 40 cm, and stays closed to cars; plain fusion gave 3.6 plus or minus 0.9 cm, open to cars.

**Clearing a conflict.** A conflict clears when five consecutive direct readings, each observed at least 30 minutes after the deeper side was reported, agree with the cell's current mean within `3 x sqrt(sigma_meas_cell^2 + sigma_meas_reading^2)` (measurement sigmas, defined below). A reading that does not agree resets the count. Clearing removes the variance floor and the conflict mean, so the agreeing readings can then show the street as clear. If nothing clears it, a conflict lapses with the evidence, 12 hours after it began.

**Measurement uncertainty.** Each track also carries the variance of the same fusion with no growth with time, plus the 8 cm offset for neighbour tracks. Its square root is `sigma_meas` (`depth_sigma_meas_cm` in the API). Blocking uses it with a bounded growth term; the displayed sigma and the unknown flag keep the full sigma, which grows without bound.

**Late readings.** A reading older than the cell's state is carried forward to the state time with the same prediction step and then fused. With one earlier state the result is identical to receiving the readings in time order, and a unit test checks this. With three or more readings it is an approximation: the carried reading and the state both contain the process noise of the interval they share, so the fused sigma comes out a few percent smaller than in time order (2.62 cm against 2.73 cm for readings of 30, 24 and 28 cm an hour apart, pinned by a unit test).

**Neighbours.** Each reading also informs the six cells of its k = 1 ring. The depth next door differs from the depth here by an unknown offset with a standard deviation of 8 cm (0.3 m/s for velocity). That offset is the same for every report from the same neighbour, so it does not average away. Each cell therefore keeps one track per adjacent cell, fuses that neighbour's readings into it at their own variance, and adds the 8 cm offset in quadrature once, when the cell is read. One report from a neighbour counts with variance `R + 8^2`, as a single independent observation would, but any number of reports from the same neighbour never count for less than `8^2`. Fifty reports of a dry street cannot outvote one direct report of deep water in the underpass beside it: a unit test checks that the underpass stays above 50 cm. Neighbour evidence is counted separately (`n_neighbour`).

**Input floors.** A reading's standard deviation is floored at 1 cm for depth and 0.05 m/s for velocity before fusion, so one over-confident report cannot pin a cell, and every variance is capped at 10^12 so it stays finite and can be written to the snapshot.

**Unknown.** A cell is `unknown` when its depth standard deviation, predicted to now, exceeds 10 cm, or when no evidence has arrived for 6 hours. Unknown does not mean passable: an unknown cell that still holds evidence is judged by the same blocking rule as a known one (see [Safety model](#safety-model)).

**History.** Each cell keeps its latest 48 direct readings (`ENGINE_HISTORY_POINTS`, 0 to 1000), each with the cell's direct estimate just after it, for the recession view. Neighbour evidence is not recorded there, and routing never reads the history.

The whole map keeps at most 250,000 history points (`ENGINE_HISTORY_MAX_TOTAL_POINTS`). A point takes 48 bytes in memory, so the cap is about 12 MB, up to about twice that while the per-cell queues hold spare capacity; on disk each point is a JSON object of roughly 150 bytes, so a full history file is about 37 MB. When the total goes over the cap it is trimmed to 90 percent of the cap: the oldest points over all cells go first, ties going to the cell whose latest reading is oldest, and a cell's most recent reading is never removed. Trimming sorts the candidates (24 bytes each, about 6 MB at the default cap) and runs once per 25,000 new points; it also runs after the snapshots are loaded. `/v1/summary` reports the current total and the cap as `history_points`. The history is saved in its own file on a slower cadence (see [Persistence](#persistence)).

**Expiry.** Each track (the cell's own readings, and the readings from each adjacent cell) expires 12 hours after its latest reading and then stops counting. When every track has expired the cell is `expired`: unknown, never blocking, and still reporting `last_depth_cm` (the direct level just after the latest direct reading) and `last_reading_age_h`, so a map can say "last report 40 cm, 13 h ago". Cells with no evidence for 48 hours are dropped from memory.

The noise rates, neighbour offset, thresholds and windows are engineering assumptions for Chennai street flooding. They are not fitted to data and are all configurable.

## Safety model

The engine must never present a flooded road as safe. These rules carry that, each with a unit or HTTP test.

1. **Every route is advisory** (`advisory: true`), and every explanation says so.
2. **One blocking rule.** A cell with a direct reading in force, known or unknown, is impassable for a vehicle when

   ```
   depth_block = max(mean, conflict_high_mean)
               + 1.5 x min(sigma_meas, 10 cm)
               + min(6 cm x sqrt(age in hours), 5 cm)
   ```

   is strictly greater than the vehicle's depth limit, or `depth_block` times velocity is strictly greater than its depth-velocity limit. `age` is the time since the latest evidence in force. Velocity is `mean + 1.5 x min(sigma, 0.5 m/s)` when measured, and never less than the 1 m/s default once it is too uncertain to show. Pedestrians use the hazard class at `depth_block`. The caps are `ENGINE_BLOCK_SIGMA_CAP_CM` and `ENGINE_BLOCK_GROWTH_CAP_CM`.
3. **Neighbour evidence blocks on its mean alone.** A cell whose only evidence in force comes from adjacent cells is impassable only when the neighbour-derived mean itself is over the limit, with no sigma or growth added. A 30 cm report closes the six surrounding cells to cars (not to buses); a 2.2 cm or a 14 cm report does not.
4. **Monotonic.** More measurement uncertainty or more age never unblocks a cell before it expires, and a conflicting report never unblocks one either; a unit test sweeps mean, measurement sigma, growth and the unknown flag for every vehicle. Because the measurement sigma is capped at 10 cm and growth at 5 cm, a vague report of a dry street (mean 0) reaches at most 15 cm, which is not greater than the car limit, so it does not block.
5. **What that means for single reports**, with the usual 3 cm sigma: a 3 cm report never blocks a car before it expires (at most 3 + 4.5 + 5 = 12.5 cm). A 10 cm report sits at 14.5 cm at the instant it is made and blocks a car within the first minute, as growth passes 0.5 cm, until it expires; with a 4 cm sigma it blocks at once. A 14 cm report blocks a car at every trust from 1 down to 0.000001.
6. **No optimistic recession.** The level is held; only the uncertainty grows. An old deep report keeps blocking until it expires: a 40 cm report blocks cars and buses for the full 12 hours.
7. **Expiry is explicit.** After 12 hours without a reading a track expires. When all of a cell's tracks have expired it becomes unknown, stops blocking, keeps the unknown-cell routing penalty, and keeps its last level and age for display and for route explanations.
8. **Conflicts never unblock.** While a conflict is in force the cell blocks on the deeper side of it. A 40 cm report followed by one dry report ten minutes later stays closed to cars. It reopens only after five consecutive agreeing reports, each at least 30 minutes after the deep one, or when the conflict lapses at 12 hours.
9. **Unknown is never safe.** Cells with no report, stale cells and expired cells are `unknown` and cost 1.5 times their length, so routes prefer streets with recent evidence of shallow water. Many reports from one neighbouring cell never count for less than the 8 cm neighbour offset.
10. **Route text names old water.** Routes report `max_known_depth_cm` (the deepest mean among known cells) and `max_unknown_depth_cm` with `max_unknown_depth_age_h` (the deepest last level among unknown cells that still carry one). When a route crosses unknown cells whose last report was too deep for the vehicle, the explanation says how many, how deep and how old, and tells the traveller to treat them as flooded.
11. **Inputs are bounded.** Readings dated more than 5 minutes ahead are refused; sigmas are floored; variances stay finite so a snapshot can always be reloaded.
12. **Trust and network.** `trust` is computed server-side by the gateway. The engine has no authentication, so it must only listen on the internal network: `ENGINE_BIND` defaults to `127.0.0.1:8302`, and the engine logs a warning at start when it is bound to anything other than a loopback or private address.

The control-room analytics (hotspots, summary, recession and export) only read the map. None of them can open or close a road: the hotspot score and the drain rate are aids for people, and rule 2 stays the only thing routing obeys. In particular a falling drain rate never shortens rule 6's hold on an old deep report.

## Hazard rating

```
HR = d x (v + 0.5) + DF
```

with `d` in metres, `v` in m/s and `DF` the debris factor. Sources, checked against the published documents:

- Surendran, Gibbs (Environment Agency), Wade, Udale-Clarke (HR Wallingford), *Supplementary note on flood hazard ratings and thresholds for development planning and control purpose*, May 2008. It gives the formula with `n = 0.5` and reconciles the two reports below.
- Defra and Environment Agency, *The Flood Risks to People Methodology*, FD2321/TR1, 2006: Table 3.1 (debris factors) and Table 3.2 (classes).
- Defra and Environment Agency, *Framework and Guidance for Assessing and Managing Flood Risk for New Development*, FD2320/TR2, 2005: Table 13.1.

**Debris factor.** The default is the FD2320 precautionary rule: DF = 0.5 for depths up to and including 0.25 m, and 1.0 above, whatever the land use. A photo cannot tell us how much debris the water carries. The FD2321 land-use table is available with `ENGINE_DEBRIS_RULE`:

| Depth | Pasture/Arable | Woodland | Urban |
|---|---|---|---|
| 0 to 0.25 m | 0 | 0 | 0 |
| 0.25 to 0.75 m | 0 | 0.5 | 1 |
| over 0.75 m, or v over 2 m/s | 0.5 | 1 | 1 |

**Classes** (FD2320 thresholds; each boundary belongs to the higher class, as coloured in Table 4 of the 2008 note):

| HR | Class | Meaning |
|---|---|---|
| below 0.75 | `low` | Caution |
| 0.75 to 1.25 | `danger_some` | Danger for some: children, the elderly and the infirm |
| 1.25 to 2.0 | `danger_most` | Danger for most, including the general public |
| 2.0 and above | `danger_all` | Danger for all, including the emergency services |

FD2321 put the top boundary at 2.5; FD2320 lowered it to 2.0 as a precaution, and the engine follows FD2320.

Two choices are ours, not the source's: a dry street (depth 0) has HR = 0, and when velocity has not been measured the engine assumes 1.0 m/s and sets `velocity_assumed: true`. That default is an engineering judgement.

## Vehicle limits

A vehicle may cross a cell that holds evidence only when the blocking depth (rule 2 of the [Safety model](#safety-model), or the neighbour mean for a cell with only neighbour evidence) is within its depth limit, and that depth (m) times the upper velocity (or the default) is within its depth-velocity limit.

| Vehicle | Depth limit | D x V limit | Basis |
|---|---|---|---|
| car | 15 cm | 0.30 m2/s | NWS: "Six inches of water will reach the bottom of most passenger cars causing loss of control and possible stalling." ARR small passenger vehicle D x V limit. |
| auto | 15 cm | 0.30 m2/s | No auto-rickshaw source found; treated like a small car. Unverified. |
| two_wheeler | 15 cm | 0.30 m2/s | No two-wheeler source found; car figure used as a floor. Unverified. |
| bus | 30 cm | 0.60 m2/s | No bus source found. NWS: "A foot of water will float many vehicles." ARR large 4WD D x V. Unverified. |
| truck | 30 cm | 0.60 m2/s | A diesel tanker or heavy goods vehicle, for access to critical sites. AIDR Guideline 7-3, Tables 1 and 2: above 0.5 m still water or D x V 0.6 m2/s, water is "unsafe for vehicles" of every size. The D x V limit is that figure; the depth limit keeps the NWS one-foot figure, 20 cm under the ceiling. |
| pedestrian | hazard class | | Blocked at `danger_some` and above, since a router cannot know whether the walker is a child or elderly. |

Sources:

- US National Weather Service, *Turn Around Don't Drown*, https://www.weather.gov/tsa/hydro_tadd (checked 26 October 2026).
- Smith, Modra, Tucker and Cox, *Vehicle Stability Testing for Flood Flows*, WRL Technical Report 2017/07, UNSW Water Research Laboratory, May 2017, Table ES-1, which confirms the interim Australian Rainfall and Runoff Project 10 thresholds (after Shand et al., 2011): small passenger vehicles 0.3 m still water and D x V at most 0.3 m2/s; large passenger vehicles 0.4 m and 0.45; large 4WD 0.5 m and 0.6. These are the depths at which vehicles start to float or slide, which is why the engine's driving limits sit lower.

- Australian Institute for Disaster Resilience, *Australian Disaster Resilience Guideline 7-3: Technical flood risk management guideline: Flood hazard*, 2014 (CC BY-NC), Table 1 (H2 "unsafe for small vehicles", H3 "unsafe for vehicles, children and the elderly") and Table 2 (H2: D x V at most 0.6 m2/s, still water at most 0.5 m), https://knowledge.aidr.org.au/media/1891/guideline-7-3-technical-flood-risk-management.pdf (checked 30 October 2026). It has no class for heavy vehicles; its H3 boundary is the ceiling for a tanker, not a crossing depth.

No Indian government or Indian Roads Congress figure for a safe crossing depth was found during the build. If one exists it should replace the three unverified rows.

Speeds used for `eta_min` (pedestrian 4, two-wheeler 20, auto 18, car 20, bus 15, truck 15 km/h) are rough assumptions, not measured city values. The same limits and speeds apply in every city.

## Cities

The engine covers a list of cities, each with an id, a display name, a metro bounding box and a road extract. The H3 grid is global, so readings from every city share one map; a city scopes queries and chooses the road graph.

- A reading is accepted when it falls inside any city's box.
- A route is planned on the graph of a city whose box holds both ends (with custom boxes that overlap, the first that holds both). An end outside every box, or ends in two different cities, is a 400 that says which.
- `/v1/cells`, `/v1/stream`, `/v1/hotspots` and `/v1/summary` take an optional `city`; `/v1/export` requires one. A cell is on a city's map when its centre lies in the city's box widened by three cell edge lengths (about 200 m at resolution 10). A reading is accepted anywhere inside the box, but its cell's centre can lie up to one edge length outside it and the six neighbours it informs up to about 2.7, so without the margin a deep report just inside the edge was fused and closed roads yet never reached that city's map, stream, summary, hotspots or export. A cell's `city` property is the city holding its centre, else the city whose widened box holds it. `/v1/cells` with an explicit `bbox` still selects cells by centre.
- All graphs load in parallel at start. A city whose graph fails still accepts readings and shows its map; only its routes answer 503, naming the city. The failure is logged as an error at start.
- `/readyz` answers 200 once the primary city's graph is loaded (`ENGINE_PRIMARY_CITY`, Chennai by default, else the first city listed). Its body lists every city's graph. When another city is still loading or has failed, the status is `degraded` and `reason` says which, but the service stays ready, so one bad extract cannot take every city offline. While the primary graph is loading or has failed, `/readyz` is 503.

**Metro boxes.** Each box covers the continuously built-up area that floods together, not the administrative district, and was drawn by hand from OpenStreetMap. The boxes do not overlap, and each fits in one `/v1/cells` request (at most 1 square degree).

| id | Box (minLon,minLat,maxLon,maxLat) | Covers | Cut from |
|---|---|---|---|
| chennai | 80.10,12.85,80.33,13.24 | Greater Chennai Corporation, Tambaram to Ennore | southern zone |
| mumbai | 72.77,18.88,73.13,19.32 | Island city, western and eastern suburbs, Thane, Navi Mumbai to Belapur | western zone |
| bengaluru | 77.44,12.82,77.80,13.15 | BBMP inside the outer ring, Electronic City to Yelahanka | southern zone |
| hyderabad | 78.24,17.22,78.66,17.62 | GHMC and Secunderabad, inside the outer ring road | southern zone |
| kolkata | 88.20,22.42,88.52,22.72 | Kolkata Municipal Corporation, Howrah, Salt Lake and New Town | eastern zone |
| delhi | 76.84,28.40,77.35,28.89 | National Capital Territory of Delhi | northern zone |

**Extracts.** Each city's roads are cut from the Geofabrik India zone extracts (downloaded 28 October 2026) with the box padded by 0.03 degrees on every side, so roads that cross the edge stay whole, then filtered to highway ways. For Mumbai:

```sh
curl -4 -LO https://download.geofabrik.de/asia/india/western-zone-latest.osm.pbf
osmium extract -s complete_ways -b 72.74,18.85,73.16,19.35 western-zone-latest.osm.pbf -o mumbai-bbox.osm.pbf
osmium tags-filter mumbai-bbox.osm.pbf w/highway -o data/osm/mumbai-roads.osm.pbf
```

The Chennai extract predates this and was clipped to 80.06,12.79,80.34,13.28.

**Graph sizes**, as built by the engine (release build, `/v1/cities` and the start-up log, 28 October 2026). Memory is the heap held by the graph's vectors (`RoadGraph::memory_bytes`).

| City | Highway ways in extract | Ways kept | Nodes | Directed edges | Segments | Memory | Parse | Build |
|---|---|---|---|---|---|---|---|---|
| chennai | 99,897 | 83,902 | 316,734 | 709,504 | 355,037 | 17.6 MiB | 179 ms | 450 ms |
| mumbai | 121,123 | 79,086 | 449,384 | 964,586 | 484,701 | 24.1 MiB | 289 ms | 714 ms |
| bengaluru | 230,786 | 191,703 | 690,647 | 1,545,473 | 775,145 | 38.3 MiB | 503 ms | 1,110 ms |
| hyderabad | 206,897 | 178,437 | 717,772 | 1,626,212 | 816,296 | 40.2 MiB | 451 ms | 1,204 ms |
| kolkata | 115,349 | 107,129 | 790,434 | 1,674,077 | 837,997 | 41.2 MiB | 352 ms | 1,287 ms |
| delhi | 237,273 | 204,342 | 976,869 | 2,201,301 | 1,103,715 | 54.2 MiB | 660 ms | 738 ms |
| **all six** | | | 3,941,840 | 8,721,153 | 4,372,891 | **215.5 MiB** | | |

Loaded in parallel, the six graphs took 2.2 s of wall time and the server answered `/readyz` 3.7 s after start, with 369 MiB resident (`ps -o rss`); the rest of the resident memory is the allocator keeping what the decode freed, the runtime and the binary. That is under 5 percent of an 8 GB laptop. These figures are from one start on an 8 GB M1 MacBook Air with other work running (load average about 3), so they are indicative rather than benchmarks. Parse and build times overlap because the cities load at the same time.

A city is added without code: list it in `ENGINE_CITIES` and give it `ENGINE_CITY_<ID>_BBOX` (and optionally `_NAME` and `_OSM_PATH`).

## Routing

Each city's road graph is built from `data/osm/<city>-roads.osm.pbf` with the `osmpbf` crate.

- Every node on a kept way is a graph node, and each pair of consecutive nodes is a segment, stored once with its length and the H3 cells it passes through (sampled every quarter of a hex edge).
- Directed edges point at segments, so both directions of a two-way street share one segment. Out-edges are stored in compressed sparse row form, so the graph is a handful of flat vectors.
- Access is per vehicle and per direction. Pedestrians use everything except motorways, in both directions. Motor vehicles skip footways, paths, steps, pedestrian streets and cycleways. Two-wheelers and autos skip motorways. Buses skip service roads and living streets. Trucks skip living streets and honour `hgv`.
- A node on a `motorway`, `trunk`, `primary` or `secondary` way (or their links) is marked as on a main road, in a spare bit of its access mask, for critical-site access routes.
- `access`, `foot`, `motor_vehicle`, `motorcar`, `motorcycle`, `psv` and `bus` tags are honoured. `oneway` (including `-1`), implied one-way on motorways and roundabouts, and `oneway:bus=no` are honoured for motor vehicles.
- Route ends snap to the nearest node, within 1.5 km, that lies in the vehicle's largest connected component, so a request never starts on a disconnected scrap of road.
- When both ends snap to the same node there is no route to plan, but the response still reports the water in that node's cell, and `passable` is false when it is too deep.

Sizes for every city are in [Cities](#cities). On the Chennai extract (99,897 highway ways and 387,959 nodes in the file) the engine keeps 83,902 ways that some vehicle may use. That gives 316,734 graph nodes, 355,037 segments, 709,504 directed edges and 470,587 segment-to-cell references. The graph's vectors hold 18,407,658 bytes (17.6 MiB), as reported by `RoadGraph::memory_bytes` and `/readyz`. The whole release process measured 88 MiB resident (`ps -o rss`) once the graph was loaded; the rest is the allocator keeping memory freed after the decode, the Tokio runtime and the binary. Decoding takes about 125 ms and building about 215 ms in the release build; the server is ready about one second after start.

Each request runs A* with a haversine heuristic up to three times:

1. **Baseline:** shortest distance, ignoring water.
2. **Water-aware:** edges through impassable cells are removed. Edges through unknown cells that do not block cost 1.5 times their length but stay usable, so routes prefer streets with recent reports of shallow water. Unknown cells with evidence in force are judged by the same blocking rule as known ones, so uncertainty never turns evidence of deep water into a green light.
3. **Least-bad fallback,** only when no dry path exists: impassable edges are allowed again at a steep cost that grows with how far the water exceeds the limit. The response says `passable: false` and explains why.

When the map holds no cell that is clear for the vehicle, every edge costs at least 1.5 per metre, so the heuristic is scaled by 1.5 and stays admissible. This makes routing on a sparsely reported map much faster.

`cells_avoided` counts impassable cells on the baseline route that the recommended route avoids. `max_depth_cm_on_route` (also returned as `max_known_depth_cm`) is the deepest mean among known cells on the route, with `max_depth_sigma_cm` beside it. `max_unknown_depth_cm` and `max_unknown_depth_age_h` give the deepest last level among unknown cells on the route that still carry one, and `old_deep_cells_crossed` counts those whose last level was too deep for the vehicle; the explanation then adds a sentence such as "1 of the unknown cells had reports of water too deep for a car that are now out of date, the deepest 45 cm reported 13 hours ago; treat it as flooded until you can see otherwise."

## API

The contract is `docs/CONTRACTS.md` section 2. All errors are JSON, `{"error": "..."}`. Invalid input is 400 with a message naming the field; an oversized body (over 16 KiB) is 413; routing before the graph has loaded is 503; no road connection is 404.

Readings are validated: lat and lon inside any covered city's box, `depth_cm` from 0 to 500, `depth_sigma_cm` above 0 (fused as at least 1 cm), `velocity_sigma_mps` required when `velocity_mps` is given, `observed_at` no more than 24 hours old and no more than 5 minutes in the future (times up to 5 minutes ahead are clamped to server time, so no cell is ever dated in the future; a reading dated hours ahead would otherwise hold its cell at that time and make every real report arrive late and decayed), `source` one of `photo`, `video`, `voice`, `official`, and `trust` in (0, 1]. A repeated `reading_id` is not fused twice; the response says `"duplicate": true`. The last 100,000 ids are remembered in memory only, so a retry that arrives after a restart is fused again.

The examples below are real responses from the release binary, trimmed where marked.

Fuse a reading:

```sh
curl -s -X POST http://127.0.0.1:8302/v1/readings -H 'content-type: application/json' -d '{
  "reading_id": "r_demo1", "lat": 12.9915, "lon": 80.2180,
  "depth_cm": 42, "depth_sigma_cm": 3, "velocity_mps": 0.4, "velocity_sigma_mps": 0.2,
  "observed_at": "2026-10-27T09:11:14Z", "source": "photo", "trust": 0.9}'
```

```json
{"reading_id":"r_demo1","cell":"8a618c4e2d47fff","depth_cm":42.0,"depth_sigma_cm":3.2,"depth_upper_cm":46.8,
 "velocity_mps":0.4,"velocity_sigma_mps":0.21,"unknown":false,"n_readings":1,"conflicts":0,"updated_at":"2026-10-27T09:11:14Z",
 "hazard":{"rating":1.38,"class":"danger_most","class_upper":"danger_most","debris_factor":1.0,
           "velocity_mps":0.4,"velocity_assumed":false},
 "duplicate":false}
```

`depth_sigma_cm` is 3.2 rather than 3 because trust 0.9 divides the variance by 0.9. `depth_upper_cm` is the depth routing judges the cell at: 42 plus 1.5 times the 3.16 cm measurement sigma, plus 0.1 cm of growth for the second between the observation time and the response. `class_upper` is the hazard class at that upper estimate.

An invalid reading:

```sh
curl -s -X POST http://127.0.0.1:8302/v1/readings -H 'content-type: application/json' \
  -d '{"lat":13.5,"lon":80.2,"depth_cm":10,"depth_sigma_cm":3,"observed_at":"2026-10-27T09:11:14Z"}'
```

```json
{"error":"lat/lon (13.5, 80.2) is outside the service area: it is not inside any covered city (chennai 80.1,12.85,80.33,13.24; mumbai 72.77,18.88,73.13,19.32; ...)"}
```

Readings, cells and routes now also carry `city`. A route between two cities is refused:

```sh
curl -s 'http://127.0.0.1:8302/v1/route?from=18.9398,72.8355&to=22.5839,88.3426&vehicle=car'
```

```json
{"error":"from is in Mumbai and to is in Kolkata; a route must start and end in the same city"}
```

Cells in a box (one feature shown):

```sh
curl -s 'http://127.0.0.1:8302/v1/cells?bbox=80.21,12.98,80.23,13.00'
```

```json
{"type":"FeatureCollection","truncated":false,"generated_at":"...","features":[
 {"type":"Feature","id":"8a618c4e2d47fff",
  "geometry":{"type":"Polygon","coordinates":[[[80.21821,12.990811],[80.218229,12.991533],[80.217619,12.991902],
     [80.216992,12.991549],[80.216973,12.990827],[80.217582,12.990458],[80.21821,12.990811]]]},
  "properties":{"cell":"8a618c4e2d47fff","depth_cm":42.0,"depth_sigma_cm":3.2,"depth_upper_cm":46.8,
     "velocity_mps":0.4,"velocity_sigma_mps":0.21,"velocity_assumed":false,
     "hazard_rating":1.38,"hazard_class":"danger_most","hazard_class_upper":"danger_most","debris_factor":1.0,
     "depth_sigma_meas_cm":3.2,"conflict_high_mean_cm":null,
     "unknown":false,"expired":false,"n_readings":1,"n_neighbour":0,"conflicts":0,
     "updated_at":"2026-10-27T09:11:14Z","last_depth_cm":42.0,"last_reading_age_h":0.0}}]}
```

Only cells with some evidence are returned. Any cell not in the response has no reports and must be drawn as unknown.

A route for a car from Velachery railway station to Guindy, after the reading above (coordinates trimmed):

```sh
curl -s 'http://127.0.0.1:8302/v1/route?from=12.9791,80.2209&to=13.0067,80.2206&vehicle=car'
```

```json
{"type":"Feature","geometry":{"type":"LineString","coordinates":[[80.22113,12.979179],"..."]},
 "properties":{"vehicle":"car","route_kind":"water_aware","distance_m":4171.0,"eta_min":12.5,
   "max_depth_cm_on_route":null,"max_depth_sigma_cm":null,"max_known_depth_cm":null,
   "max_unknown_depth_cm":null,"max_unknown_depth_age_h":null,"old_deep_cells_crossed":0,"cells_on_route":37,"cells_avoided":1,
   "unknown_cells_crossed":37,"impassable_cells_crossed":0,"passable":true,"advisory":true,
   "depth_limit_cm":15.0,"pedestrian_block_class":null,"safety_sigmas":1.5,"computed_at":"...",
   "explanation":"Advisory only. This route avoids 1 map cell on the shortest path with water that may be deeper than 15 cm, or flowing too fast, for a car. It is 659 m (19%) longer than the shortest route. None of the 37 map cells along it has a recent report, so conditions on it are unknown. Check conditions as you go and turn back if the water looks deeper than expected."},
 "baseline":{"type":"Feature","geometry":{"type":"LineString","coordinates":["..."]},
   "properties":{"route_kind":"baseline","distance_m":3512.0,"impassable_cells_crossed":1,"advisory":true,"...":"..."}}}
```

The baseline is a GeoJSON foreign member named `baseline` on the route Feature, with the same properties. `route_kind` is `water_aware`, `least_bad` (no dry route exists; `passable` is false) or `baseline`.

Live changes:

```sh
curl -sN http://127.0.0.1:8302/v1/stream
```

```text
event: cell
data: {"type":"Feature","id":"8a618c4e2d47fff","geometry":{...},"properties":{...}}
```

Each reading produces seven `cell` events: its own cell and the six neighbours. A client that falls more than 4096 events behind receives a comment line saying so and should refetch `/v1/cells`.

Health:

```sh
curl -s http://127.0.0.1:8302/readyz
```

With every graph loaded the status is `ready`. Started with Delhi's extract missing, the release binary answered 200 with (trimmed to two cities):

```json
{"status":"degraded","primary_city":"chennai","cells":0,
 "reason":"road graphs failed, so routes there answer 503: delhi: cannot read OSM extract /nonexistent/delhi.pbf: No such file or directory (os error 2)",
 "cities":{"chennai":{"status":"ready","ways":83902,"nodes":316734,"edges":709504,"segments":355037,
   "segment_cell_refs":470587,"memory_bytes":18407658,"memory_mib":17.6,"parse_ms":166,"build_ms":315},
  "delhi":{"status":"failed","error":"cannot read OSM extract /nonexistent/delhi.pbf: No such file or directory (os error 2)"},"...":"..."}}
```

## Control-room analytics

These endpoints are read-only views for people running a city's response. They are advisory and never change routing.

### Cities

`GET /v1/cities` lists each city with its box, centre, the number of cells with state, and its graph (`ready` with sizes and load times, `loading`, or `failed` with the error):

```json
{"cities":[{"id":"mumbai","name":"Mumbai","bbox":[72.77,18.88,73.13,19.32],"centre":{"lat":19.1,"lon":72.95},"cells":14,
  "graph":{"status":"ready","ways":79086,"nodes":449384,"edges":964586,"segments":484701,"segment_cell_refs":622455,
           "memory_bytes":25242322,"memory_mib":24.1,"parse_ms":289,"build_ms":714}}, "..."]}
```

### Hotspots

`GET /v1/hotspots?city=mumbai&limit=20` ranks the cells with evidence in force (expired cells are left out) by a score from 0 to 100, the sum of four parts:

```
depth     = 40 x min(depth_block / 60 cm, 1)
hazard    = 0, 10, 20 or 30 for low, danger_some, danger_most, danger_all at depth_block
freshness = 20 x max(0, 1 - age_h / 12)
reports   = 10 x min(independent_reports / 5, 1)
```

- `depth_block` is the depth routing judges the cell at (rule 2 of the [Safety model](#safety-model): the deeper side of any conflict, plus the measurement margin and growth with time; the neighbour mean for neighbour-only cells). 60 cm is twice the bus limit; past it a street is closed to everything and more depth adds no urgency.
- `hazard` is the Defra class at `depth_block` and the blocking velocity (the 1 m/s default when not measured).
- `age_h` is the time since the latest evidence in force. Evidence expires at 12 hours, so freshness reaches zero there.
- `independent_reports` counts direct readings plus adjacent cells whose readings are in force. Many reports from one neighbour count once, as in the fusion model.

Depth and hazard carry 70 of the 100 points, so a fresh report of a dry street (about 25 points) never outranks deep water a few hours old. Ties go to the deeper `depth_block`, then the lower cell id. The weights are a judgement about what a control room should look at first, not fitted values. `limit` is 1 to 200 (default 20); `city` is optional. Each hotspot lists its parts, the vehicles it is closed to and one plain-English reason per part, plus notes for conflicts, neighbour-only evidence, unknown cells and assumed velocity. A real answer, trimmed:

```json
{"cell":"8a608b1996effff","lat":19.017451,"lon":72.856336,"score":77.2,
 "parts":{"depth":35.2,"hazard":20.0,"freshness":20.0,"reports":2.0},
 "depth_block_cm":52.8,"depth_cm":48.0,"depth_sigma_cm":3.2,"hazard_class_upper":"danger_most","age_h":0.0,
 "independent_reports":1,"unknown":false,"blocks":["pedestrian","two_wheeler","auto","car","bus"],
 "reasons":["Water may be as deep as 53 cm here (mean 48 cm plus the safety margin): 35.2 of 40 points.",
            "At that depth the Defra hazard is danger for most people: 20 of 30 points.",
            "The latest evidence is 0.0 hours old: 20.0 of 20 points.",
            "1 independent report (1 in the cell, 0 from adjacent cells): 2 of 10 points.",
            "Flow speed was not measured, so 1.0 m/s is assumed.",
            "Closed to every vehicle and to people on foot."]}
```

### Summary

`GET /v1/summary?city=kolkata` counts every cell with state in the city (all cities without `city`):

- `by_class`: cells by the class shown on the map (`unknown` includes stale and expired cells);
- `unknown`, `stale` (no evidence for 6 hours, not yet expired) and `expired`;
- `cells_in_conflict` (a conflict in force) and `conflicting_readings` (all conflicts counted);
- `blocked`: cells closed to each vehicle under rule 2;
- `needs_fresh_report`: stale or expired cells whose last direct level was deeper than 15 cm, the lowest vehicle limit, with the 50 deepest listed (cells are forgotten after 48 hours, so these are all recent);
- `readings` and `newest_reading_at`;
- `history_points`: `total` and `max`, the depth history held over the whole map (all cities, not just this one) and its cap.

### Recession

`GET /v1/recession?cell=<H3 index>` returns a cell's depth history (its latest direct readings, oldest first, each with the cell's direct estimate after it), its current properties, and a drain rate when there is enough data.

The drain rate is a weighted least-squares straight line through the direct readings of the last 6 hours before the newest one, weighted by `1 / sigma^2` of each reading as fused. It needs at least 3 readings spanning at least 30 minutes. Its standard error is the textbook one for known variances, scaled up by the square root of the reduced chi-squared when the readings scatter more than their sigmas say. `trend` is `falling` or `rising` when the rate is more than twice its standard error, otherwise `steady`. When falling, `hours_to_15_cm` is how long the fitted line takes to reach 15 cm from its value at the newest reading.

What it is not: a forecast. It is a straight line through a handful of reports that may come from different spots in a 66 m cell and from different people, and recession is rarely linear. It is shown so a control room can see whether water is going down, and it never opens a road. Four readings of 50, 40, 30 and 20 cm an hour apart give, as the release binary answered:

```json
{"drain":{"rate_cm_per_h":10.0,"rate_sigma_cm_per_h":1.41,"trend":"falling","points_used":4,
          "from":"2026-10-28T13:22:57Z","to":"2026-10-28T16:22:58Z","hours_to_15_cm":0.5},
 "drain_note":"A weighted straight-line fit to this cell's direct readings over the last 6 hours. It describes the recent past, is not a forecast, and never opens a road."}
```

The cell keeps blocking cars all the while: its blocking depth is still above 15 cm. A cell with too few readings returns `"drain": null` and the reason in `drain_note`. An invalid index or one at another H3 resolution is a 400; a cell no report has touched is a 404.

### Export

`GET /v1/export?city=mumbai&format=csv` (or `format=geojson`, the default) returns every cell with state in the city as an attachment named `neermattam-<city>-<UTC time>.<ext>`. The GeoJSON is the `/v1/cells` collection; the CSV has one row per cell with the cell centre and the same properties, empty where a value is null:

```text
cell,city,lat,lon,depth_cm,depth_sigma_cm,depth_upper_cm,depth_sigma_meas_cm,conflict_high_mean_cm,velocity_mps,velocity_sigma_mps,velocity_assumed,hazard_rating,hazard_class,hazard_class_upper,debris_factor,unknown,expired,n_readings,n_neighbour,conflicts,updated_at,last_depth_cm,last_reading_age_h
8a608b0a69b7fff,mumbai,19.017386,72.85518,48.0,8.6,48.0,8.6,,,,true,1.72,danger_most,danger_most,1.0,false,false,0,1,0,2026-10-28T16:22:57Z,,
```

Exports are capped at `ENGINE_MAX_CELLS` rows. Under the cap, rows are in cell-index order. When the cap applies, the cells are first sorted by hotspot score, highest first (then by blocking depth, then cell index, with cells whose evidence has expired last), so the deepest and most dangerous cells are always kept, and the rows come out in that order. The `X-Neermattam-Truncated` and `X-Neermattam-Total-Cells` headers say whether the cap was reached and how many cells the city has; the GeoJSON also carries `truncated` and `total_cells` members. The CSV header line is always just the column names.

**Cost.** Hotspots, the summary, the export and the city list run on the blocking thread pool, so a large map never stalls the workers that take readings and plan routes. Hotspots score every cell but write the explanation only for the cells returned. On a worst-case Chennai map, with a reading every 0.001 degrees so that all 68,295 cells in the box hold state, building the views took about 23 ms, ranking the top 20 hotspots about 7 ms (about 140 ms before the explanation was limited to the cells returned) and the summary about 3 ms (`cargo bench --bench engine -- analytics_full`, measured with other work running, load average about 10, so indicative only).

## Critical sites

The full description, data sources, limits and how an operator uses it are in docs/SITES.md; the contract is in docs/CONTRACTS.md section 2. In the engine:

- `sites.rs` holds the sites: OpenStreetMap ones from `assets/sites-osm.json` (written by `scripts/extract_sites.py`, embedded in the binary, or read from `ENGINE_SITES_OSM_PATH`), kept only when they lie in a configured city's box, and operator ones added through the gateway, each visible only to its owner. A watch zone is every map cell whose centre is within a ring's radius; a ring is `over` when any cell with evidence in force has a blocking depth over the threshold, `under` only when at least half its cells are known, and otherwise `unknown`. Access routes call `plan_route` from the site to up to three of the nearest main-road nodes and fuel stations, for the truck, the two-wheeler and the car, and keep the best.
- `ledger.rs` is the risk ledger: per H3 resolution 8 area (`ENGINE_LEDGER_RESOLUTION`), the readings, wet readings (fused level at or over 15 cm), India Standard Time days with water, the peak, and wet spells per map cell from the first wet reading to the first dry one, with spells that go quiet for 48 hours counted as unresolved. It is fed from `POST /v1/readings`, keeps readings with a `source` apart from those without, is never trimmed by the forget window, and never feeds routing.
- `sites_api.rs` serves `/v1/sites`, `/v1/sites/{id}` and `/v1/siting` on the blocking pool. Listing Chennai's 834 hospitals with both rings took about 100 ms, and one site's six access routes about 100 ms, on the local release build with the demo map (one run, indicative).

## Persistence

The map is saved as two files, each written to a temporary file in the same directory, flushed to disk and renamed over the old one, so a crash mid-write never leaves a half-written file.

- **Cell snapshot**, `data/engine/cells.json` (set `ENGINE_SNAPSHOT_PATH`; empty disables persistence). Every cell of every city with its full state, which is what routing reads. Written at most once every 5 seconds (`ENGINE_SNAPSHOT_INTERVAL_SECS`), only when something changed. Format version 5.
- **History file**, next to it with `.history.json` in place of `.json` (`data/engine/cells.history.json`). Every cell's depth history for the recession view. It can be many times larger than the cell snapshot, so it is written on its own slower cadence, at most once every 60 seconds (`ENGINE_HISTORY_SNAPSHOT_SECS`), only when the history changed. Format `neermattam-engine-history` version 1, with the same format, version and resolution checks as the cell snapshot.

- **Risk ledger**, `cells.ledger.json` beside it, written with the cell snapshot's cadence when it changed, and **operator sites**, `cells.sites.json`, written whenever one is added or removed. A file that cannot be read is moved aside on its own; nothing else is lost.

Both are written once more on shutdown, cell state first. A crash therefore loses at most one minute of history and at most one snapshot interval of cell state. On start the cell snapshot is loaded first, then the history file. Any cell whose history does not reach its latest direct reading (the readings made after the last history save, or a missing history file) gets that reading back as one history point marked `migrated`, rebuilt from the level and time the cell state keeps; migrated points are shown but left out of drain rates, so a crash costs the recession view at most a minute of readings and never its latest level. History for a cell the map no longer holds is skipped.

Older cell snapshots are migrated: version 4 kept the history inline and is read with it (the next history save moves it to the history file); version 3 had no history, so each cell gets one migrated point from its latest direct level. A cell snapshot written at a different H3 resolution or by a version older than 3, one that cannot be parsed, or one holding a cell with no evidence or a number that is not finite, is moved aside and the engine starts with an empty map. A history file that is invalid in the same ways is moved aside on its own: the cell state is kept, and each cell keeps its latest level as its only history point.

Shutdown on SIGTERM or Ctrl-C is graceful: open SSE streams are closed, in-flight requests finish (up to 10 seconds), and the final snapshot is written.

## Running

```sh
cd engine
cargo run --release                      # listens on 127.0.0.1:8302
ENGINE_LOG_FORMAT=pretty RUST_LOG=debug cargo run --release
```

The road graphs load in the background, all cities in parallel; `/readyz` returns 503 with a reason until the primary city's graph is ready, then 200 (`degraded` while any other city is loading or has failed), and `/v1/cities` shows each city's progress. Logs are structured JSON by default.

| Variable | Default | Meaning |
|---|---|---|
| `ENGINE_BIND` | `127.0.0.1:8302` | Listen address; internal network only (a warning is logged otherwise) |
| `ENGINE_CITIES` | `chennai,mumbai,bengaluru,hyderabad,kolkata,delhi` | Cities to cover, in lookup order |
| `ENGINE_OSM_DIR` | `../data/osm` | Folder holding `<city>-roads.osm.pbf` |
| `ENGINE_CITY_<ID>_BBOX` | built-in box | Override a city's box, or give a new city its box (at most 1 square degree) |
| `ENGINE_CITY_<ID>_NAME` | built-in name, else the id | Display name |
| `ENGINE_CITY_<ID>_OSM_PATH` | `<ENGINE_OSM_DIR>/<id>-roads.osm.pbf` | A city's road extract |
| `ENGINE_OSM_PATH`, `ENGINE_BBOX` | | Older names for Chennai's extract and box, still honoured |
| `ENGINE_SNAPSHOT_PATH` | `../data/engine/cells.json` | Snapshot file; empty disables |
| `ENGINE_SNAPSHOT_INTERVAL_SECS` | `5` | Minimum seconds between snapshots |
| `ENGINE_H3_RESOLUTION` | `10` | H3 resolution, 7 to 12 |
| `ENGINE_HALF_LIFE_HOURS` | `off` | Opt-in recession half-life; `off` holds the level |
| `ENGINE_DEPTH_NOISE_CM_PER_SQRT_HOUR` | `6` | Depth process noise |
| `ENGINE_VELOCITY_NOISE_MPS_PER_SQRT_HOUR` | `0.2` | Velocity process noise |
| `ENGINE_NEIGHBOUR_SIGMA_CM` | `8` | Extra sigma for ring cells |
| `ENGINE_NEIGHBOUR_VELOCITY_SIGMA_MPS` | `0.3` | Extra velocity sigma for ring cells |
| `ENGINE_UNKNOWN_SIGMA_CM` | `10` | Depth sigma above which a cell is unknown |
| `ENGINE_UNKNOWN_VELOCITY_SIGMA_MPS` | `0.5` | Velocity sigma above which velocity is unknown |
| `ENGINE_STALE_HOURS` | `6` | Hours without evidence before a cell is unknown |
| `ENGINE_EXPIRE_HOURS` | `12` | Hours after its latest reading that evidence stops counting |
| `ENGINE_FORGET_HOURS` | `48` | Hours without evidence before a cell is dropped |
| `ENGINE_HISTORY_POINTS` | `48` | Direct readings kept per cell for `/v1/recession` (0 to 1000) |
| `ENGINE_HISTORY_MAX_TOTAL_POINTS` | `250000` | History points kept over the whole map |
| `ENGINE_HISTORY_SNAPSHOT_SECS` | `60` | Minimum seconds between history file writes |
| `ENGINE_PRIMARY_CITY` | `chennai` (else the first city) | City whose graph `/readyz` waits for |
| `ENGINE_SAFETY_SIGMAS` | `1.5` | k in the blocking rule |
| `ENGINE_BLOCK_SIGMA_CAP_CM` | `10` | Depth sigma cap in the blocking rule |
| `ENGINE_BLOCK_VELOCITY_SIGMA_CAP_MPS` | `0.5` | Velocity sigma cap in the blocking rule |
| `ENGINE_BLOCK_GROWTH_CAP_CM` | `5` | Cap on growth with time in the blocking rule |
| `ENGINE_UNKNOWN_PENALTY` | `1.5` | Cost multiplier through unknown cells |
| `ENGINE_DEFAULT_VELOCITY_MPS` | `1.0` | Velocity assumed when not measured |
| `ENGINE_PEDESTRIAN_BLOCK_CLASS` | `danger_some` | Lowest class that blocks walkers |
| `ENGINE_DEBRIS_RULE` | `fd2320` | `fd2320`, `fd2321_urban`, `fd2321_woodland` or `fd2321_pasture` |
| `ENGINE_MAX_SNAP_M` | `1500` | Furthest a route end may be from a road |
| `ENGINE_MAX_OBSERVATION_SKEW_HOURS` | `24` | How old `observed_at` may be; at most 5 minutes ahead is always allowed |
| `ENGINE_BODY_LIMIT_BYTES` | `16384` | Request body limit |
| `ENGINE_REQUEST_TIMEOUT_SECS` | `15` | Timeout for non-streaming requests |
| `ENGINE_MAX_CELLS` | `20000` | Most features one `/v1/cells` call returns, and most rows one export returns |
| `ENGINE_LOG_FORMAT` | `json` | `json` or `pretty`; filter with `RUST_LOG` |
| `ENGINE_SITES_PER_OWNER` | `100` | Operator sites one API key may add |
| `ENGINE_SITES_MAX` | `10000` | Operator sites in all |
| `ENGINE_LEDGER_RESOLUTION` | `8` | H3 resolution of the risk ledger's areas (5 to 10, not finer than the map) |
| `ENGINE_SITES_OSM_PATH` | embedded | An OpenStreetMap site file to use instead of the embedded `assets/sites-osm.json` |

## Tests

```sh
cd engine
cargo test                                   # 135 tests: unit, HTTP and real-graph
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```

- Unit tests cover the fusion maths, holding the level and the opt-in recession, expiry, conflicting readings and how a conflict clears, measurement sigma, out-of-order readings, neighbour inflation, trust, the unknown rules, the monotonic blocking rule and its bounded growth, neighbour-only blocking, the hazard formula against cells of Table 4 of the 2008 note, class boundaries, the FD2321 debris table, OSM access and one-way rules, snapping, and snapshot round trips.
- Routing tests on a synthetic street grid show the route bending around a flooded cell, a bus crossing water that blocks a car, and the least-bad fallback when every path is flooded.
- `tests/chennai.rs` loads the real extract, routes Velachery railway station (12.9791, 80.2209) to Guindy (13.0067, 80.2206), seeds 50 cm of water at the middle of the shortest path and checks the new route avoids it. It takes about 0.4 s. When the extract is missing the test prints how to build it and passes; set `NEERMATTAM_REQUIRE_OSM=1` to make that a failure.
- `tests/multicity.rs` loads all six real extracts in parallel, checks every city reports ready, routes a car across each city through the HTTP API (Chennai Central to Velachery, CSMT to Bandra, Majestic to MG Road, Secunderabad to Charminar, Howrah to Sealdah, New Delhi station to India Gate), seeds 50 cm of water at the middle of each shortest path and checks each route goes around it, and checks a Mumbai to Kolkata request is refused. It takes about 2.5 s. A missing extract is skipped with a message unless `NEERMATTAM_REQUIRE_OSM=1`.
- `tests/cities.rs` covers multi-city behaviour and the control-room endpoints on synthetic street grids: city tagging, cross-city and out-of-coverage routes, loading and failed graphs, city-scoped cells, stream and export, a deep reading on a city's edge reaching every city-scoped view, readiness following the primary city, a capped export keeping the most dangerous cells, the global history total in the summary, hotspot ordering and reasons, summary counts, the recession history and drain rate, and every new 400 and 404.
- Unit tests in `analytics.rs` pin the hotspot ordering (deep and fresh first, more reports higher, deep water hours old above fresh shallow water), summary counts, and the drain-rate fit (exact rate and error on a clean line, refusals on thin data, scatter widening the error). `persist.rs` checks the round trip of both files, a crash between history saves (cell state intact, the lost readings' latest level recovered), a bad history file never costing the cell state, the version 4 and version 3 migrations, refuses version 2, refuses truncated and corrupt cell snapshots whole, and orders and bounds a loaded history; `tests/http.rs` checks the two cadences; `fusion.rs` checks the history is bounded and in time order; `config.rs` checks the city variables.
- `tests/sites.rs` covers critical sites over HTTP on a flooded synthetic grid: open routes on a dry map, water that detours a car and a two-wheeler but not a tanker, deep water at the site closing every route, the list's order and validation, operator sites private to their owner and reloaded after a restart, and the risk ledger keeping unattributed (simulated) readings out of the reported record. Unit tests in `sites.rs` and `ledger.rs` cover ring cells, unknown versus known rings, old deep water, the trend, fuel candidates, the embedded file, wet spells, India dates and unresolved spells; `vehicle.rs` checks the truck limit against the AIDR ceiling and `graph.rs` truck access and main-road candidates.
- `tests/http.rs` drives the full router with `tower::ServiceExt`: health, readiness, validation messages, body limit, duplicates, GeoJSON cells, routes, the SSE stream and its shutdown, and a snapshot restart.

## Benchmarks

`cargo bench --bench engine` (criterion). The routing cases are skipped when the extract is missing.

Measured on an 8 GB M1 MacBook Air (Apple M1, 8 cores), release profile, Rust 1.97.1, 27 October 2026, with other work paused. Criterion reports a confidence interval; the middle value is shown.

The timings below were measured at commit f8e8fb6. Since then the engine has gained per-neighbour tracks, the conflict check and per-track expiry, which add a small allocation per newly touched cell and a few more fusions per cell read, so the ingestion figures in particular are expected to be somewhat higher. They have not been refreshed because the machine was heavily loaded throughout the review (load average 18 to 40 on 8 cores), and timings taken that way would not be honest. The route kinds printed by the benchmark were rechecked under the new rules and are unchanged: with 200 readings every trip still finds a dry route, with 2000 none does. Rerun `cargo bench --bench engine` with other work paused to refresh the tables.

**Ingestion**

| Benchmark | Time per reading | Readings per second |
|---|---|---|
| `Grid::ingest` (cell, six neighbours, fusion) | 1.11 us | about 904,000 |
| `POST /v1/readings` through the full router in process (JSON parse, validation, fusion, seven SSE events, response) | 46 us | about 21,700 |

The HTTP figure has no network or TLS in it; one Tokio current-thread runtime drives the router directly.

**Graph load:** 330 ms to decode the PBF and build the graph, including the per-segment cells.

**Routing** (car; one request runs the baseline and the water-aware search, plus the fallback when needed). The flooded maps use random readings spread over the whole service area, from 0 to 80 cm deep.

| Trip | Dry map | 200 readings | 2000 readings |
|---|---|---|---|
| Velachery station to Guindy, 3.5 km | 0.28 ms | 1.3 ms | 29 ms |
| T. Nagar to Sholinganallur, 19.6 km | 7.8 ms | 15 ms | 79 ms |
| Chennai Central to Tambaram, 27.2 km | 14 ms | 33 ms | 36 ms |

With 200 readings every trip still has a dry route (`water_aware`). With 2000 random readings so much of the city is blocked that no trip has one, so those cases are the worst case: the search first explores everything reachable to prove there is no dry path, then runs the least-bad fallback. Through HTTP, the same T. Nagar to Sholinganallur request on a nearly empty map took 6 to 9 ms end to end with curl.

To rerun: `cd engine && cargo bench --bench engine`. The routing cases print the route kind for each flooded map before timing.
