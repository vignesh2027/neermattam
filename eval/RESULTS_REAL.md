# Real-photo evaluation

**There is no ground truth for these photos.** Nobody measured the water in them, so nothing here is an accuracy figure. What this shows is qualitative behaviour on real street photos: what the models located, what the verify-and-refine agent accepted or rejected and why, which method answered, and the real latency and cost. Accuracy figures for the geometry come from the synthetic evaluation in `docs/GEOMETRY.md`; field accuracy needs photos with measured depths.

Rerun from `perception/` with `uv run --extra local python ../eval/real_photos.py --modal` (live: Muse Spark Open on Baseten, SAM 2.1 hiera-large on the Modal CPU service). The SAM size comparison is `../eval/sam_ab.py` (table in `RESULTS_SAM_AB.md`) and the latency breakdown is `../eval/latency.py`. The photos are downloaded from Wikimedia Commons into `eval/photos/` and are not in git; their source, author and licence are in `eval/commons_photos.json` and repeated per photo below. Overlays, response JSON and every image the verifier was shown go to `eval/out/` (also not in git). The set is the four photos named in the brief (c1 to c4) plus two extra Commons photos (x1, x2) with motorcycles standing in water side-on.

## What changed in this milestone

* SAM is prompted with the box **and** a ring of points on the tyre rubber, with negative points on the hub and on what touches the tyre (exhaust, mudguard, wall); the scene model is asked for those points. A box alone let a wall leak into the c3 scooters on the Modal probe.
* All of SAM's proposals (box with points, points alone) are closed into solid wheel shapes (a tyre ring is closed with its convex hull) and ranked by how well the outline fits one ellipse (`wheel_shape.py`). The best two go to Muse Spark Open as two images, A and B; it picks one and judges it.
* An outline is measured only if Muse Spark Open calls it good on a close crop, its shape score is at least 0.35, and its bounding box overlaps the tyre box Muse draws on the same crop by at least 0.7 (IoU).
* Water regions confirmed as water are kept when the agent refines towards more water.
* SAM 2.1 can run on the Modal service (hiera-large, the default when `MODAL_SAM_URL` is set, with local hiera-small as the fallback) or locally (small, base-plus, large, fp32 or fp16).
* Muse Spark Open runs the scene read at reasoning effort `low` (Baseten's lowest for this model; the default is `xhigh`), images go compressed (1024 px at JPEG quality 75; check crops at 512 px), and scene readings are cached by photo, prompt and settings.
* `POST /v1/measure/stream` sends each step as server-sent events, then the result.

## What the runs show

These notes come from looking at the overlays and verifier images of this run, of the five arms of the SAM comparison, and of the latency runs.

* **No wheel measurement passes the judges on these six photos.** Every final answer is the scene model's labelled estimate (`method: model_estimate`, sigma 20 to 42 cm): c1 45 cm, c2 25 cm, c3 95 cm, c4 140 cm, x1 23 cm, x2 22 cm. By eye these are plausible (c1 people about knee deep, c3 a man chest deep, x1 and x2 the lower quarter to third of the tyres under water). Across runs the same photo's estimate moved: c3 between 90 and 125 cm, c4 between 120 and 180 cm, the others by 5 to 15 cm. The stated sigma (30 percent) covers most of that, not all of it.
* **Why the wheels fail.** On the side-on motorcycles of x1 and x2 the proposals either take in the exhaust, swingarm, fork or mudguard (SAM treats the metal attached to the wheel as part of it) or follow only the near half of the tyre. With a hand-placed ring of points on the front tyre of x2, hiera-large did give a full-wheel outline (plus the mudguard), so the masks can be right; the prompts Muse Spark Open gives are not yet precise enough to get there. Its boxes on the full photo usually land on the reflection below a tyre (about 85 px low on a 2048 px photo), and on crops its tyre boxes and points still miss by tens of pixels.
* **The judges stop wrong numbers.** Muse Spark Open alone called partial outlines "good" many times (for example `eval/out/ab-large/x2_agent/two_wheeler_wheel_2_try2.jpg`, panel B covers only the left half of the rear tyre); the box-agreement check rejected them. Before that check existed, a development run measured x2 at 2.8 and 3.5 cm from half-tyre outlines, which is clearly too shallow; those numbers never reached a result in the final code.
* **Head-on tyres** (c2's oncoming motorcycle, one bike in x1) are recognised and dropped at once.
* **Water masks** are accepted on every photo with wheels. The confirmed-water union now keeps the first verified region when the agent refines towards more water (in an earlier c2 run the refinement had replaced a 16 percent mask with a 2 percent one).

## SAM 2.1 sizes (from `RESULTS_SAM_AB.md`)

Same scene readings for every arm, 10 wheels checked per arm. Every arm accepted the same 1 wheel (the scooter cut off by c2's left edge, whose bottom is outside the photo, so the geometry correctly declined it), except Modal, which accepted none; no arm reached a geometry depth. Medians of per-photo time were 41 s (small), 53 s (base-plus), 57 s (large), 33 s (large fp16) and 79 s (large on Modal CPU), with the scene reading cached. Peak memory: 0.4 to 0.5 GB of process memory and 2.3 to 2.8 GB on the GPU for fp32, 1.5 GB GPU for large in fp16 (1.3 GB process peak while converting). Base-plus pushed this 8 GB machine into swap once (270 s for one photo). On these photos the larger models did not change the outcome, so the size is not the limiting factor; the prompts are.

## Latency

Before this milestone (27 October run, scene effort `xhigh`, 1280 px images): c1 17 s, c2 46 s, c3 18 s, c4 14 s, x1 110 s, x2 67 s.

After, measured on 28 October with `latency.py` (local SAM small, fresh scene readings): c3 11 s and c4 15 s in one run each, but most photos took 120 to 370 s, because Baseten answered image requests very slowly during those runs: the probe call with one 140 to 210 KB image and a one-line answer took 50 to 107 s for most photos, against 4 to 12 s for the others and 6 s earlier in the day. A text-only probe stayed at 1 to 2 s, and an upload of a similar file to another host ran at 44 KB/s, so the delay was on Baseten's side. Under normal service the critical path is the scene reading (8 to 11 s at effort `low`, against 23 s at `xhigh` for the same photo) plus one round of mask (1 to 3 s locally, 12 to 23 s on Modal CPU) and check (6 to 12 s) per refinement. A photo whose scene has no wheel answers in about 10 to 15 s; a photo with one or two wheels took 30 to 52 s with local SAM when the service was normal and the scene reading was cached (8 to 11 s more for a fresh reading), so **the 30 s target is met only when the first proposal is accepted**, and not with SAM on the Modal CPU deployment (80 to 370 s), which needs the GPU deploy.

## What still fails

* No real-photo wheel measurement passes the judges yet; see above. The next lever is precise prompts: a human-placed ring of points gave a usable large-model outline on x2, Muse Spark Open's points did not.
* Muse Spark Open's judgements are noisy at low reasoning effort (it called half-tyre outlines good), so the numeric checks carry the weight.
* Latency is set by Baseten's image handling, which was erratic during these runs.
* The scene model's own estimates have no calibration and moved by up to 60 cm between runs on the same photo (c4).

<!-- real-eval:begin -->
Generated by `uv run --extra local python ../eval/real_photos.py` on 2026-10-28 with muse-spark-open (meta-models/muse-spark-open-1026 on Baseten, scene reasoning effort low) and sam-2.1-hiera-large (Modal) on Modal CPU (ready in 21.5 s). No ground truth exists for these photos; only qualitative behaviour is shown.

### c1: Chennai flood water logging near ofice nandanam4.jpg (named in the brief)

Author Vijayingarsal, licence CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/), source https://commons.wikimedia.org/wiki/File:Chennai_flood_water_logging_near_ofice_nandanam4.jpg.

Result: **model_estimate**, depth 45.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 45.0 cm (sigma 20.0 cm), basis: Knee-deep water on adults and half-submerged two-wheeler wheels across street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two-wheeler wheel 1: the scene model did not confirm the mask (outline and the tyre's box disagree); two-wheeler wheel 2: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Knee-deep water on adults and half-submerged two-wheeler wheels across street.

Wall time 182.6 s (timings ms: scene 66552, agent 115999, geometry 0); model cost USD 0.051878.

Overlay: `../eval/out/final-modal/c1_overlay.png`. Verifier images: `../eval/out/final-modal/c1_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1632x918 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel, person, person, person; own estimate 45 cm | segment and verify 2 wheels and the water | 66552 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [796,474,856,510] px + points [801,489], [826,470], [849,487] - points [826,479], [865,487] | 6 proposals, 6 distinct; best shapes 0.26, 0.00 | verify the best two | 12638 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [596,234,1056,694] px with 2 outlines A and B | chose B, shape 0.00 (fit 0.90, arc 42 deg, ratio 0.39, spill 0.37), box agreement 0.37, is_target=false, outline wrong, view head_on, edge visible 0.35, waterline visible; A includes riders and bodywork, B is closer but still does not follow the tyre outer edge above water and includes exhaust/bodywork, so neither is a clean tyre  | refine with box [795,476,858,511] px + points [799,497], [825,481], [853,495] - points [827,499], [871,493], [826,464] | 7766 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [978,443,1013,470] px + points [981,454], [994,444], [1007,454] - points [994,453], [997,439] | 6 proposals, 6 distinct; best shapes 0.70, 0.48 | verify the best two | 21535 |
| segment | water | sam-2.1-hiera-large (Modal) | box [401,610,1068,879] px + points [[666, 744]] | mask 181936 px (12% of photo) | verify | 28948 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [795,476,858,511] px + points [799,497], [825,481], [853,495] - points [827,499], [871,493], [826,464] | 6 proposals, 6 distinct; best shapes 0.21, 0.13 | verify the best two | 11348 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on turbulent brown floodwater surface in foreground contiguous with water around wading people and vehicles. | accept | 9068 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [698,336,954,592] px with 2 outlines A and B | chose B, shape 0.13 (fit 0.91, arc 129 deg, ratio 0.61, spill 0.16, closed ring), box agreement 0.28, is_target=false, outline partial, view head_on, edge visible 0.30, waterline visible; B is closer to rear wheel but its magenta outline still includes red clothing and bodywork beyond the narrow head-on tyre edge instead of following tyre outer e | refine with box [804,483,842,507] px + points [808,495], [823,488], [837,495] - points [823,499], [823,509], [850,488] | 18990 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [869,328,1125,584] px with 2 outlines A and B | chose B, shape 0.48 (fit 1.00, arc 99 deg, ratio 0.58, spill 0.03), is_target=false, outline wrong, view head_on, edge visible 0.45, waterline visible; Both outlines bulge onto mudguard/exhaust and bodywork on the right side instead of following the narrow head-on tyre band, B is slightly tighter but still wron | refine with box [978,443,1013,470] px + points [978,473], [991,455], [1005,473] - points [992,471], [1016,468], [991,487] | 38908 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [804,483,842,507] px + points [808,495], [823,488], [837,495] - points [823,499], [823,509], [850,488] | 6 proposals, 6 distinct; best shapes 0.44, 0.00 | verify the best two | 11069 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [978,443,1013,470] px + points [978,473], [991,455], [1005,473] - points [992,471], [1016,468], [991,487] | 6 proposals, 6 distinct; best shapes 0.50, 0.21 | verify the best two | 11247 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [690,335,946,591] px with 2 outlines A and B | chose B, shape 0.00 (fit 1.00, arc 54 deg, ratio 0.37, spill 0.69, closed ring), box agreement 0.68, is_target=true, outline good, view side, edge visible 0.55, waterline visible; Panel B outline follows the rear tyre outer edge above water with straight waterline while Panel A includes riders and bodywork. | give up: the outline covers a different area than the tyre (0.68) | 10202 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [830,339,1103,612] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.00, waterline not visible; Neither magenta outline follows the rear tyre; A is a large water region and B is on the tail-light/bodywork, tyre is submerged under muddy water with no outer  | refine with box [978,443,1013,470] px - points [1018,465], [915,471], [966,492] | 9543 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [978,443,1013,470] px - points [1018,465], [915,471], [966,492] | 3 proposals, 3 distinct; best shapes 0.38, 0.00 | verify the best two | 11533 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [869,327,1125,583] px with 2 outlines A and B | chose B, mask too small, box agreement 0.00, is_target=true, outline good, view head_on, edge visible 0.60, waterline visible; Panel B's narrow upright magenta outline follows the rear tyre's outer edges above the water while Panel A includes the whole mudguard and bodywork | give up: tyre seen head-on, its outline cannot be measured | 23020 |
| estimate | photo | muse-spark-open | no measurable wheel | 45 cm, sigma 20 cm | report model estimate | 0 |

### c2: India - Chennai - Monsoon - 04 (3058215637).jpg (named in the brief)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3058215637/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_04_(3058215637).jpg.

Result: **model_estimate**, depth 25.0 cm, sigma 20.0 cm, confidence 0.22.
Scene model's own estimate: 25.0 cm (sigma 20.0 cm), basis: Water submerges half of centre road depth with kerb and distant auto wheels showing 20-35cm.
Captured at (EXIF): 2008-10-22T10:04:35.

Reasons: standing water visible; two-wheeler wheel 1: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); two-wheeler wheel 2: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water submerges half of centre road depth with kerb and distant auto wheels showing 20-35cm.

Wall time 235.1 s (timings ms: scene 63514, agent 171488, geometry 0); model cost USD 0.053365.

Overlay: `../eval/out/final-modal/c2_overlay.png`. Verifier images: `../eval/out/final-modal/c2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel, kerb; own estimate 25 cm | segment and verify 2 wheels and the water | 63514 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [1739,725,1813,873] px + points [1757,740], [1782,742], [1794,776], [1755,793] - points [1778,791], [1778,790], [1817,822] | 6 proposals, 6 distinct; best shapes 0.48, 0.38 | verify the best two | 15002 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1565,607,1949,991] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.65, waterline visible; Neither magenta outline follows the motorcycle front tyre; panel A marks a small ramp edge and panel B marks the whole concrete ramp, both off the narrow head-o | refine with box [1714,711,1747,840] px + points [1718,759], [1728,720], [1741,760] - points [1730,774], [1728,707], [1687,783] | 8923 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [0,1163,142,1531] px + points [25,1218], [66,1233], [92,1316], [27,1347] - points [49,1355], [49,1356], [109,1330] | 6 proposals, 6 distinct; best shapes 0.37, 0.37 | verify the best two | 25537 |
| segment | water | sam-2.1-hiera-large (Modal) | box [451,897,1986,1522] px + points [[1151, 1247]] | mask 943680 px (30% of photo) | verify | 27390 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [0,869,549,1536] px with 2 outlines A and B | chose A, shape 0.37 (fit 0.93, arc 78 deg, ratio 0.41, spill 0.00, closed ring), box agreement 0.19, is_target=false, outline wrong, view side, edge visible 0.90, waterline not visible; Both magenta outlines follow the green mudguard/bodywork and plate area, not the black tyre outer edge, so neither is the tyre itself and would give wrong depth | refine with box [0,1283,89,1528] px + points [13,1428], [31,1311], [69,1340], [78,1431] - points [27,1440], [42,1364], [58,1281] | 10689 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [1714,711,1747,840] px + points [1718,759], [1728,720], [1741,760] - points [1730,774], [1728,707], [1687,783] | 6 proposals, 6 distinct; best shapes 0.47, 0.47 | verify the best two | 13520 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [0,1283,89,1528] px + points [13,1428], [31,1311], [69,1340], [78,1431] - points [27,1440], [42,1364], [58,1281] | 6 proposals, 5 distinct; best shapes 0.26, 0.23 | verify the best two | 13976 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta overlay covers the flooded street surface in the foreground contiguous with the water around the motorcyclist. | accept | 38123 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1547,583,1919,954] px with 2 outlines A and B | chose B, shape 0.47 (fit 1.00, arc 84 deg, ratio 0.44, spill 0.00, closed ring), box agreement 0.00, is_target=false, outline wrong, view head_on, edge visible 0.60, waterline visible; Panel B follows the spare tyre leaning on the wall, not the motorcycle front wheel, so it cannot measure depth from the vehicle wheel. | refine with box [1835,740,1874,838] px + points [1842,805], [1850,753], [1864,801] - points [1854,788], [1852,758], [1898,805] | 61656 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [0,1066,384,1536] px with 2 outlines A and B | chose A, shape 0.26 (fit 0.63, arc 120 deg, ratio 0.91, spill 0.00), box agreement 0.54, is_target=true, outline good, view side, edge visible 0.62, waterline visible; Outline A follows the black tyre outer curve tightly above the straight waterline without including mudguard or bodywork. | refine with box [0,1283,54,1503] px + points [5,1371], [22,1307], [48,1366], [48,1458] - points [16,1457], [44,1319], [64,1321] | 55853 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [1835,740,1874,838] px + points [1842,805], [1850,753], [1864,801] - points [1854,788], [1852,758], [1898,805] | 6 proposals, 6 distinct; best shapes 0.38, 0.25 | verify the best two | 13206 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [0,1283,54,1503] px + points [5,1371], [22,1307], [48,1366], [48,1458] - points [16,1457], [44,1319], [64,1321] | 6 proposals, 6 distinct; best shapes 0.53, 0.50 | verify the best two | 13106 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1719,653,1986,920] px with 2 outlines A and B | chose B, shape 0.25 (fit 0.96, arc 48 deg, ratio 0.32, spill 0.00), box agreement 0.10, is_target=true, outline good, view head_on, edge visible 0.65, waterline visible; B correctly traces the narrow head-on front tyre edge above water while A balloons onto water and reflection. | give up: tyre seen head-on, its outline cannot be measured | 9150 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [0,1068,372,1536] px with 2 outlines A and B | chose B, shape 0.50 (fit 1.00, arc 90 deg, ratio 0.62, spill 0.00, closed ring), box agreement 0.58, is_target=true, outline good, view head_on, edge visible 0.82, waterline visible; Panel B magenta follows the tyre outer edge above water with straight waterline at bottom while Panel A only outlines a tiny sliver, so B is the correct tyre ou | give up: tyre seen head-on, its outline cannot be measured | 52070 |
| estimate | photo | muse-spark-open | no measurable wheel | 25 cm, sigma 20 cm | report model estimate | 0 |

### c3: Inundation of Chennai in 2017-3.jpg (named in the brief)

Author Kaitha Poo Manam, licence CC BY 3.0 (https://creativecommons.org/licenses/by/3.0/), source https://commons.wikimedia.org/wiki/File:Inundation_of_Chennai_in_2017-3.jpg.

Result: **model_estimate**, depth 95.0 cm, sigma 28.5 cm, confidence 0.21.
Scene model's own estimate: 95.0 cm (sigma 28.5 cm), basis: waist to chest deep water on multiple adults standing on submerged street.
Captured at (EXIF): 2015-12-02T11:04:31.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: waist to chest deep water on multiple adults standing on submerged street.

Wall time 89.9 s (timings ms: scene 89807); model cost USD 0.007409.

Overlay: `../eval/out/final-modal/c3_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: person, person, person, person; own estimate 95 cm | no wheel: report own estimate | 89807 |
| estimate | photo | muse-spark-open | no measurable wheel | 95 cm, sigma 28 cm | report model estimate | 0 |

### c4: Chennai-floods-2015-dec-7.jpg (named in the brief)

Author Destination8infinity, licence CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/), source https://commons.wikimedia.org/wiki/File:Chennai-floods-2015-dec-7.jpg.

Result: **model_estimate**, depth 140.0 cm, sigma 42.0 cm, confidence 0.21.
Scene model's own estimate: 140.0 cm (sigma 42.0 cm), basis: Water submerges ground floors and overturned auto to roof level indicating deep street flooding.
Captured at (EXIF): 2015-12-02T07:56:12.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water submerges ground floors and overturned auto to roof level indicating deep street flooding.

Wall time 9.1 s (timings ms: scene 9023); model cost USD 0.002514.

Overlay: `../eval/out/final-modal/c4_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 980x735 px | water visible; references: none; own estimate 140 cm | no wheel: report own estimate | 9023 |
| estimate | photo | muse-spark-open | no measurable wheel | 140 cm, sigma 42 cm | report model estimate | 0 |

### x1: India - Chennai - Monsoon - 07 (3059060932).jpg (extra, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059060932/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_07_(3059060932).jpg.

Result: **model_estimate**, depth 23.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 23.0 cm (sigma 20.0 cm), basis: Water reaches lower quarter to third of upright two-wheeler wheels across the flooded lane.
Captured at (EXIF): 2008-10-22T10:06:36.

Reasons: standing water visible; two-wheeler wheel 1: the scene model did not confirm the mask (Panel A outline is seat/bodywork and Panel B outline encloses whole scooter bodywork, neither follows the round tyre edge so neither is the tyre.); two-wheeler wheel 2: the scene model did not confirm the mask (A follows the tyre edge on the right side but misses most of the tyre circumference, while B traces water and reflection below the tyre.); two-wheeler wheel 3: the scene model did not confirm the mask (Both magenta outlines mark water/ground at the tyre base not the tyre outer edge, so neither follows the narrow head-on tyre band above the waterline.); two-wheeler wheel 4: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches lower quarter to third of upright two-wheeler wheels across the flooded lane.

Wall time 371.7 s (timings ms: scene 3, agent 371586, geometry 0); model cost USD 0.08257.

Overlay: `../eval/out/final-modal/x1_overlay.png`. Verifier images: `../eval/out/final-modal/x1_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel; own estimate 23 cm | segment and verify 5 wheels and the water | 3 |
| segment | two wheeler wheel 4 | sam-2.1-hiera-large (Modal) | box [1819,551,2048,1144] px + points [1853,790], [1894,684], [1995,670] - points [2005,912], [2017,971], [1810,740], [2044,590] | 6 proposals, 6 distinct; best shapes 0.34, 0.22 | verify the best two | 14779 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [760,654,1127,1036] px + points [786,831], [885,716], [1020,740], [1067,869] - points [920,876], [918,917], [754,895], [1020,639] | 6 proposals, 6 distinct; best shapes 0.37, 0.16 | verify the best two | 24161 |
| segment | water | sam-2.1-hiera-large (Modal) | box [4,920,1859,1532] px + points [[936, 1335]] | mask 1121100 px (36% of photo) | verify | 29686 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-large (Modal) | box [1364,619,1598,1024] px + points [1393,759], [1450,674], [1534,710], [1556,825] - points [1475,856], [1475,923], [1354,730], [1567,630] | 6 proposals, 5 distinct; best shapes 0.67, 0.41 | verify the best two | 32817 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [0,0,1972,1536] px with 2 outlines A and B | chose B, shape 0.16 (fit 0.63, arc 87 deg, ratio 0.47, spill 0.04, closed ring), box agreement 0.10, is_target=false, outline wrong, view side, edge visible 0.50, waterline visible; Panel B outline covers the whole moped body, exhaust and seat instead of just the tyre, while Panel A is only a small engine fragment, so B is closer but still  | refine with box [564,802,1065,1048] px + points [604,986], [696,863], [918,860], [1026,975] - points [815,940], [835,819], [894,765] | 9614 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta covers open floodwater but misses the water immediately around the motorcycles and people where they stand. | refine with box [8,458,1819,1020] px + point [829, 754] | 7456 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [459,639,765,1029] px + points [492,826], [563,722], [684,743] - points [598,854], [604,911], [432,871], [729,659] | 6 proposals, 6 distinct; best shapes 0.54, 0.32 | verify the best two | 38551 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [839,233,2048,1487] px with 2 outlines A and B | chose A, shape 0.67 (fit 0.89, arc 156 deg, ratio 0.49, spill 0.00, closed ring), box agreement 0.15, is_target=false, outline wrong, view head_on, edge visible 0.60, waterline visible; Outline A follows only a small left slice of the tyre and misses most of the tyre above water so it cannot give correct water depth | refine with box [1358,666,1582,967] px + points [1375,841], [1456,691], [1539,847] - points [1456,804], [1424,634], [1595,822] | 11238 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [564,802,1065,1048] px + points [604,986], [696,863], [918,860], [1026,975] - points [815,940], [835,819], [894,765] | 6 proposals, 6 distinct; best shapes 0.36, 0.33 | verify the best two | 18294 |
| verify | two wheeler wheel 4 | muse-spark-open | crop [190,0,2048,1536] px with 2 outlines A and B | chose B, shape 0.22 (fit 1.00, arc 39 deg, ratio 0.64, spill 0.00, closed ring), box agreement 0.16, is_target=true, outline good, view head_on, edge visible 0.55, waterline visible; Panel B outline follows the narrow upright tyre edge above water at the right edge while Panel A extends into the reflection. | give up: tyre seen head-on, its outline cannot be measured | 43471 |
| segment | water | sam-2.1-hiera-large (Modal) | box [8,458,1819,1020] px + points [[829, 754]] | mask 57244 px (2% of photo) | verify | 26150 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-large (Modal) | box [1358,666,1582,967] px + points [1375,841], [1456,691], [1539,847] - points [1456,804], [1424,634], [1595,822] | 6 proposals, 6 distinct; best shapes 0.53, 0.32 | verify the best two | 27984 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [446,0,2048,1536] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.55, waterline visible; Neither magenta outline follows the narrow head-on front tyre; A is water region and B is a tiny mark at the waterline. | refine with box [1466,599,1625,943] px + points [1479,845], [1492,670], [1596,673], [1608,845] - points [1544,776], [1544,565], [1547,974] | 45147 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [0,0,2048,1536] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.65, waterline visible; Panel A outline covers water and panel B is a sliver at the waterline, neither follows the round outer edge of the tyre above water. | refine with box [817,484,1325,817] px + points [850,710], [987,551], [1197,548], [1284,691] - points [1073,679], [1106,501], [1069,886] | 70150 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only a tiny sliver under bikes, missing most of the floodwater in foreground and around yellow box. | refine with box [61,1075,1741,1521] px + point [819, 1321] | 69671 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [58,293,1122,1356] px with 2 outlines A and B | chose A, shape 0.54 (fit 0.86, arc 132 deg, ratio 0.82, spill 0.00, closed ring), box agreement 0.01, is_target=true, outline good, view side, edge visible 0.66, waterline visible; Panel A outline follows the outer tyre edge above the water while Panel B outlines the exhaust/hub area. | refine with box [234,630,418,879] px + points [247,756], [290,644], [375,647], [408,754] - points [320,768], [354,837], [283,837] | 126190 |
| segment | water | sam-2.1-hiera-large (Modal) | box [61,1075,1741,1521] px + points [[819, 1321]] | mask 784812 px (25% of photo) | verify | 32054 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-large (Modal) | box [1466,599,1625,943] px + points [1479,845], [1492,670], [1596,673], [1608,845] - points [1544,776], [1544,565], [1547,974] | 6 proposals, 6 distinct; best shapes 0.82, 0.38 | verify the best two | 47917 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [817,484,1325,817] px + points [850,710], [987,551], [1197,548], [1284,691] - points [1073,679], [1106,501], [1069,886] | 6 proposals, 6 distinct; best shapes 0.39, 0.20 | verify the best two | 51448 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but foreground patch misses the flooded water immediately surrounding the motorcycles. | accept (coverage partial) | 19913 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [279,0,1790,1416] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.45, waterline visible; Panel A outline is seat/bodywork and Panel B outline encloses whole scooter bodywork, neither follows the round tyre edge so neither is the tyre. | give up | 19270 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [234,630,418,879] px + points [247,756], [290,644], [375,647], [408,754] - points [320,768], [354,837], [283,837] | 6 proposals, 6 distinct; best shapes 0.44, 0.36 | verify the best two | 42770 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [1086,324,1980,1218] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.85, waterline visible; Both magenta outlines mark water/ground at the tyre base not the tyre outer edge, so neither follows the narrow head-on tyre band above the waterline. | give up | 55411 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [0,425,646,1081] px with 2 outlines A and B | chose B, shape 0.36 (fit 0.89, arc 81 deg, ratio 0.56, spill 0.00, closed ring), box agreement 0.10, is_target=true, outline good, view side, edge visible 0.65, waterline visible; Panel B magenta outline follows the rear tyre outer edge above water with straight waterline, while Panel A outlines the exhaust silencer not the tyre. | refine with box [134,553,253,790] px + points [145,674], [176,564], [233,586], [244,655] - points [189,676], [181,689], [217,589] | 77102 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [134,553,253,790] px + points [145,674], [176,564], [233,586], [244,655] - points [189,676], [181,689], [217,589] | 6 proposals, 6 distinct; best shapes 0.82, 0.57 | verify the best two | 26476 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [0,319,578,1080] px with 2 outlines A and B | chose A, shape 0.82 (fit 0.97, arc 156 deg, ratio 0.51, spill 0.00, closed ring), box agreement 0.20, is_target=false, outline partial, view side, edge visible 0.55, waterline visible; A follows the tyre edge on the right side but misses most of the tyre circumference, while B traces water and reflection below the tyre. | give up | 60113 |
| estimate | photo | muse-spark-open | no measurable wheel | 23 cm, sigma 20 cm | report model estimate | 0 |

### x2: India - Chennai - Monsoon - 02 (3059047958).jpg (extra, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059047958/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_02_(3059047958).jpg.

Result: **model_estimate**, depth 22.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 22.0 cm (sigma 20.0 cm), basis: both visible motorcycle wheels submerged to ~20-22cm on flooded carriageway.
Captured at (EXIF): 2008-10-22T09:17:24.

Reasons: standing water visible; two-wheeler wheel 1: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); two-wheeler wheel 2: the scene model did not confirm the mask (B follows the tyre edge but covers only a small sliver while A includes exhaust and bodywork, so B is better but only partial.); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: both visible motorcycle wheels submerged to ~20-22cm on flooded carriageway.

Wall time 312.8 s (timings ms: scene 109273, agent 203366, geometry 0); model cost USD 0.055604.

Overlay: `../eval/out/final-modal/x2_overlay.png`. Verifier images: `../eval/out/final-modal/x2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 22 cm | segment and verify 2 wheels and the water | 109273 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [1683,900,1965,1102] px + points [1720,1052], [1802,932], [1909,932], [1946,1026] - points [1823,1015], [1825,1014], [1862,965], [1761,980] | 6 proposals, 6 distinct; best shapes 0.40, 0.39 | verify the best two | 35062 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [1239,922,1506,1193] px + points [1266,1138], [1323,989], [1456,995], [1491,1114] - points [1401,1084], [1401,1081], [1423,1160], [1288,895] | 6 proposals, 6 distinct; best shapes 0.84, 0.52 | verify the best two | 36151 |
| segment | water | sam-2.1-hiera-large (Modal) | box [29,607,1017,1525] px + points [[446, 1000]] | mask 924216 px (29% of photo) | verify | 43051 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but misses the water directly around the submerged motorcycles on the right. | refine with box [1092,1187,1995,1508] px + point [1532, 1355] | 9741 |
| segment | water | sam-2.1-hiera-large (Modal) | box [1092,1187,1995,1508] px + points [[1532, 1355]] | mask 300916 px (10% of photo) | verify | 13902 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only a small bottom corner patch and misses the flooded street around the half-submerged motorcycle. | refine with box [164,461,1229,1198] px + point [637, 816] | 8369 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [1283,468,2048,1536] px with 2 outlines A and B | chose B, shape 0.39 (fit 0.94, arc 78 deg, ratio 0.61, spill 0.00, closed ring), box agreement 0.02, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B follows the front tyre outer edge above water with straight waterline, A encloses large water area below tyre. | refine with box [1547,783,1705,960] px + points [1560,897], [1600,799], [1663,799], [1694,893] - points [1631,898], [1643,762], [1648,848] | 46606 |
| segment | water | sam-2.1-hiera-large (Modal) | box [164,461,1229,1198] px + points [[637, 816]] | mask 623492 px (20% of photo) | verify | 14244 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [1547,783,1705,960] px + points [1560,897], [1600,799], [1663,799], [1694,893] - points [1631,898], [1643,762], [1648,848] | 6 proposals, 6 distinct; best shapes 0.66, 0.53 | verify the best two | 14759 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [1074,391,2036,1353] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.45, waterline visible; Neither magenta outline follows the wheel tyre the large polygon in A covers the whole body and the small loop in B is on the fuel tank so no outline is the tyr | refine with box [1239,1007,1413,1194] px + points [1252,1117], [1293,1034], [1367,1043], [1400,1117] - points [1326,1114], [1322,1043], [1336,957] | 13560 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-large (Modal) | box [1239,1007,1413,1194] px + points [1252,1117], [1293,1034], [1367,1043], [1400,1117] - points [1326,1114], [1322,1043], [1336,957] | 6 proposals, 6 distinct; best shapes 0.72, 0.36 | verify the best two | 15458 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [986,643,1808,1464] px with 2 outlines A and B | chose A, shape 0.84 (fit 0.92, arc 189 deg, ratio 0.70, spill 0.00, closed ring), box agreement 0.62, is_target=true, outline good, view side, edge visible 0.55, waterline visible; Outline A closely follows the outer black tyre edge above water with rim inside, while outline B traces the exhaust, so A is correct for water depth measurement | refine with box [1238,913,1437,1155] px + points [1251,1060], [1296,943], [1377,947], [1412,1041] - points [1334,1070], [1475,1017], [1259,885] | 93567 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region aligns with flooded street water surface next to partially submerged motorcycle. | accept | 54025 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [1238,913,1437,1155] px + points [1251,1060], [1296,943], [1377,947], [1412,1041] - points [1334,1070], [1475,1017], [1259,885] | 6 proposals, 6 distinct; best shapes 0.42, 0.40 | verify the best two | 17648 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1018,712,1657,1352] px with 2 outlines A and B | chose A, shape 0.42 (fit 0.81, arc 117 deg, ratio 0.85, spill 0.00, closed ring), box agreement 0.50, is_target=true, outline partial, view side, edge visible 0.60, waterline visible; Outline follows left and upper outer edge of rear tyre but lower-right closure is diagonal rather than tracing the waterline and hidden right edge. | refine with box [1240,936,1366,1202] px + points [1247,1090], [1261,1006], [1325,974], [1346,1032] - points [1308,1093], [1385,1061], [1269,930] | 11863 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-large (Modal) | box [1240,936,1366,1202] px + points [1247,1090], [1261,1006], [1325,974], [1346,1032] - points [1308,1093], [1385,1061], [1269,930] | 6 proposals, 6 distinct; best shapes 0.83, 0.64 | verify the best two | 17434 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [988,671,1754,1438] px with 2 outlines A and B | chose B, shape 0.64 (fit 0.81, arc 177 deg, ratio 0.73, spill 0.00, closed ring), box agreement 0.58, is_target=true, outline good, view head_on, edge visible 0.70, waterline visible; Panel B follows the narrow upright tyre band closely while panel A inflates to include exhaust and swingarm. | give up: tyre seen head-on, its outline cannot be measured | 14266 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [950,581,1889,1519] px with 2 outlines A and B | chose B, shape 0.36 (fit 0.89, arc 96 deg, ratio 0.65, spill 0.03, closed ring), box agreement 0.15, is_target=false, outline partial, view side, edge visible 0.65, waterline visible; B follows the tyre edge but covers only a small sliver while A includes exhaust and bodywork, so B is better but only partial. | give up | 77662 |
| estimate | photo | muse-spark-open | no measurable wheel | 22 cm, sigma 20 cm | report model estimate | 0 |

<!-- real-eval:end -->
