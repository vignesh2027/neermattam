# Neermattam engine

The engine is the Rust service at `engine/` (crate `neermattam-engine`, port 8302). It does four things:

1. Fuses street-level flood depth readings into a live hex map of Chennai, with uncertainty that grows with time.
2. Rates each cell with the UK Defra and Environment Agency flood hazard formula.
3. Loads the Chennai road network from OpenStreetMap into a compact directed graph.
4. Routes pedestrians, two-wheelers, autos, cars and buses around water each of them should not cross, and returns the plain shortest route next to it for comparison.

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
| pedestrian | hazard class | | Blocked at `danger_some` and above, since a router cannot know whether the walker is a child or elderly. |

Sources:

- US National Weather Service, *Turn Around Don't Drown*, https://www.weather.gov/tsa/hydro_tadd (checked 26 October 2026).
- Smith, Modra, Tucker and Cox, *Vehicle Stability Testing for Flood Flows*, WRL Technical Report 2017/07, UNSW Water Research Laboratory, May 2017, Table ES-1, which confirms the interim Australian Rainfall and Runoff Project 10 thresholds (after Shand et al., 2011): small passenger vehicles 0.3 m still water and D x V at most 0.3 m2/s; large passenger vehicles 0.4 m and 0.45; large 4WD 0.5 m and 0.6. These are the depths at which vehicles start to float or slide, which is why the engine's driving limits sit lower.

No Indian government or Indian Roads Congress figure for a safe crossing depth was found during the build. If one exists it should replace the three unverified rows.

Speeds used for `eta_min` (pedestrian 4, two-wheeler 20, auto 18, car 20, bus 15 km/h) are rough assumptions, not measured Chennai values.

## Routing

The road graph is built from `data/osm/chennai-roads.osm.pbf` with the `osmpbf` crate.

- Every node on a kept way is a graph node, and each pair of consecutive nodes is a segment, stored once with its length and the H3 cells it passes through (sampled every quarter of a hex edge).
- Directed edges point at segments, so both directions of a two-way street share one segment. Out-edges are stored in compressed sparse row form, so the graph is a handful of flat vectors.
- Access is per vehicle and per direction. Pedestrians use everything except motorways, in both directions. Motor vehicles skip footways, paths, steps, pedestrian streets and cycleways. Two-wheelers and autos skip motorways. Buses skip service roads and living streets.
- `access`, `foot`, `motor_vehicle`, `motorcar`, `motorcycle`, `psv` and `bus` tags are honoured. `oneway` (including `-1`), implied one-way on motorways and roundabouts, and `oneway:bus=no` are honoured for motor vehicles.
- Route ends snap to the nearest node, within 1.5 km, that lies in the vehicle's largest connected component, so a request never starts on a disconnected scrap of road.
- When both ends snap to the same node there is no route to plan, but the response still reports the water in that node's cell, and `passable` is false when it is too deep.

On the Chennai extract (99,897 highway ways and 387,959 nodes in the file) the engine keeps 83,902 ways that some vehicle may use. That gives 316,734 graph nodes, 355,037 segments, 709,504 directed edges and 470,587 segment-to-cell references. The graph's vectors hold 18,407,658 bytes (17.6 MiB), as reported by `RoadGraph::memory_bytes` and `/readyz`. The whole release process measured 88 MiB resident (`ps -o rss`) once the graph was loaded; the rest is the allocator keeping memory freed after the decode, the Tokio runtime and the binary. Decoding takes about 125 ms and building about 215 ms in the release build; the server is ready about one second after start.

Each request runs A* with a haversine heuristic up to three times:

1. **Baseline:** shortest distance, ignoring water.
2. **Water-aware:** edges through impassable cells are removed. Edges through unknown cells that do not block cost 1.5 times their length but stay usable, so routes prefer streets with recent reports of shallow water. Unknown cells with evidence in force are judged by the same blocking rule as known ones, so uncertainty never turns evidence of deep water into a green light.
3. **Least-bad fallback,** only when no dry path exists: impassable edges are allowed again at a steep cost that grows with how far the water exceeds the limit. The response says `passable: false` and explains why.

When the map holds no cell that is clear for the vehicle, every edge costs at least 1.5 per metre, so the heuristic is scaled by 1.5 and stays admissible. This makes routing on a sparsely reported map much faster.

`cells_avoided` counts impassable cells on the baseline route that the recommended route avoids. `max_depth_cm_on_route` (also returned as `max_known_depth_cm`) is the deepest mean among known cells on the route, with `max_depth_sigma_cm` beside it. `max_unknown_depth_cm` and `max_unknown_depth_age_h` give the deepest last level among unknown cells on the route that still carry one, and `old_deep_cells_crossed` counts those whose last level was too deep for the vehicle; the explanation then adds a sentence such as "1 of the unknown cells had reports of water too deep for a car that are now out of date, the deepest 45 cm reported 13 hours ago; treat it as flooded until you can see otherwise."

## API

The contract is `docs/CONTRACTS.md` section 2. All errors are JSON, `{"error": "..."}`. Invalid input is 400 with a message naming the field; an oversized body (over 16 KiB) is 413; routing before the graph has loaded is 503; no road connection is 404.

Readings are validated: lat and lon inside the service area (`80.10,12.85,80.33,13.24` by default), `depth_cm` from 0 to 500, `depth_sigma_cm` above 0 (fused as at least 1 cm), `velocity_sigma_mps` required when `velocity_mps` is given, `observed_at` no more than 24 hours old and no more than 5 minutes in the future (times up to 5 minutes ahead are clamped to server time, so no cell is ever dated in the future; a reading dated hours ahead would otherwise hold its cell at that time and make every real report arrive late and decayed), `source` one of `photo`, `video`, `voice`, `official`, and `trust` in (0, 1]. A repeated `reading_id` is not fused twice; the response says `"duplicate": true`. The last 100,000 ids are remembered in memory only, so a retry that arrives after a restart is fused again.

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
{"error":"lat/lon (13.5, 80.2) is outside the service area 80.1,12.85,80.33,13.24 (minLon,minLat,maxLon,maxLat)"}
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

```json
{"status":"ready","cells":0,"graph":{"ways":83902,"nodes":316734,"edges":709504,"segments":355037,
 "segment_cell_refs":470587,"memory_bytes":18407658,"parse_ms":125,"build_ms":214}}
```

## Persistence

Cell state is written to `data/engine/cells.json` (set `ENGINE_SNAPSHOT_PATH`; empty disables it) at most once every 5 seconds and only when something changed, plus once on shutdown. Each write goes to a temporary file in the same directory, is flushed to disk and is renamed over the old file, so a crash never leaves a half-written snapshot. On start the snapshot is loaded; a snapshot written at a different H3 resolution or by an older format version (the current one is 3), one that cannot be parsed, or one holding a cell with no evidence or a number that is not finite, is moved aside and the engine starts with an empty map.

Shutdown on SIGTERM or Ctrl-C is graceful: open SSE streams are closed, in-flight requests finish (up to 10 seconds), and the final snapshot is written.

## Running

```sh
cd engine
cargo run --release                      # listens on 127.0.0.1:8302
ENGINE_LOG_FORMAT=pretty RUST_LOG=debug cargo run --release
```

The road graph loads in the background; `/readyz` returns 503 with a reason until it is ready. Logs are structured JSON by default.

| Variable | Default | Meaning |
|---|---|---|
| `ENGINE_BIND` | `127.0.0.1:8302` | Listen address; internal network only (a warning is logged otherwise) |
| `ENGINE_OSM_PATH` | `../data/osm/chennai-roads.osm.pbf` | Road extract |
| `ENGINE_SNAPSHOT_PATH` | `../data/engine/cells.json` | Snapshot file; empty disables |
| `ENGINE_SNAPSHOT_INTERVAL_SECS` | `5` | Minimum seconds between snapshots |
| `ENGINE_H3_RESOLUTION` | `10` | H3 resolution, 7 to 12 |
| `ENGINE_BBOX` | `80.10,12.85,80.33,13.24` | Service area |
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
| `ENGINE_MAX_CELLS` | `20000` | Most features one `/v1/cells` call returns |
| `ENGINE_LOG_FORMAT` | `json` | `json` or `pretty`; filter with `RUST_LOG` |

## Tests

```sh
cd engine
cargo test                                   # unit, HTTP and real-graph tests
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```

- Unit tests cover the fusion maths, holding the level and the opt-in recession, expiry, conflicting readings and how a conflict clears, measurement sigma, out-of-order readings, neighbour inflation, trust, the unknown rules, the monotonic blocking rule and its bounded growth, neighbour-only blocking, the hazard formula against cells of Table 4 of the 2008 note, class boundaries, the FD2321 debris table, OSM access and one-way rules, snapping, and snapshot round trips.
- Routing tests on a synthetic street grid show the route bending around a flooded cell, a bus crossing water that blocks a car, and the least-bad fallback when every path is flooded.
- `tests/chennai.rs` loads the real extract, routes Velachery railway station (12.9791, 80.2209) to Guindy (13.0067, 80.2206), seeds 50 cm of water at the middle of the shortest path and checks the new route avoids it. It takes about 0.4 s. When the extract is missing the test prints how to build it and passes; set `NEERMATTAM_REQUIRE_OSM=1` to make that a failure.
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
