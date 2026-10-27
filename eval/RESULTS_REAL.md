# Real-photo evaluation

**There is no ground truth for these photos.** Nobody measured the water in them, so nothing here is an accuracy figure. What this shows is qualitative behaviour on real street photos: what the models located, what the verify-and-refine agent accepted or rejected and why, which method answered, and the real latency and cost. Accuracy figures for the geometry come from the synthetic evaluation in `docs/GEOMETRY.md`; field accuracy needs photos with measured depths.

Rerun from `perception/` with `uv run --extra local python ../eval/real_photos.py` (live: Muse Spark Open on Baseten, SAM 2.1 locally). The photos are downloaded from Wikimedia Commons into `eval/photos/` and are not in git; their source, author and licence are in `eval/commons_photos.json` and repeated per photo below. Overlays, response JSON and every image the verifier was shown go to `eval/out/` (also not in git).

The set is the four photos named in the brief (c1 to c4) plus two extra Commons photos (x1, x2) chosen because they show motorcycles standing in water side-on, so that the wheel path is exercised on real photos too.

## What the run shows

These notes come from looking at the overlays and at every verifier image in `eval/out/*_agent/`, over this run and the earlier development runs of the same photos.

* **Every photo ends with the scene model's estimate, not a measurement.** No wheel produced a usable geometry measurement on these six photos. Each answer is labelled `method: model_estimate`, with a sigma of 20 to 40 cm, and the reasons say "not a measurement". By eye the estimates are plausible: c1 35 cm (people wading shin to knee deep), c2 24 cm (a motorcycle in the main lane, water near its hub), c3 105 cm (a man in the middle of the street chest deep; if anything this reads low), c4 130 cm (shop shutters submerged; impossible to check), x1 14 cm and x2 23 cm (the lower quarter to third of the tyres under water). Over four or five runs of each photo the estimates moved by 5 to 13 cm (c3 between 105 and 115 cm, x2 between 15 and 28 cm), inside the stated sigma.
* **The scene model now skips wheels it cannot measure.** In c1, c3 and c4 it lists only people, which is right: the motorbikes in c1 are small and seen from behind, the scooters in c3 lie on their sides, and the "wheels" it named in c4 in an earlier run belonged to an overturned cart. In c2 it lists the oncoming motorcycle.
* **Its boxes for small objects are often off, and the agent catches that.** The first box for a wheel usually lands on the water or the reflection below the tyre (in c2 about 85 px low on a 2048 px photo, similarly in x1 and x2), and the probe before this work put a scooter wheel point on a water tank. In every such case the first check rejected the mask ("outline traces the water reflection below the wheel") and returned a corrected box plus points on the tyre and on the parts to leave out; the next SAM prompt landed on the right tyre.
* **Head-on tyres are dropped at once.** c2's motorcycle and one bike in x1 are seen nose-on; the verifier reports `view head_on` and the agent stops ("tyre seen head-on, its outline cannot be measured") instead of spending more calls.
* **SAM 2.1 hiera-small does not give a clean tyre outline on these photos, and that is what stops the measurement.** On the spoked and alloy wheels of x1 and x2 its masks either take in the exhaust, swingarm, fork or mudguard, or follow only the near half of the tyre. Segmenting small objects in a zoomed crop and padding the box helped visibly but not enough. The verifier rejects most of these as "partial", and only an outline it calls good is measured.
* **The verifier is not strict enough on its own.** With low reasoning effort it accepted outlines that were plainly incomplete, so every accept is now re-checked with full reasoning, which overruled one accept in this run (x1 wheel 1, found to be head-on and partial on the close look) and several partial outlines in the development runs. It still let one through: x1 wheel 4 was accepted although the mask covers only the left part of the tyre (`eval/out/x1_agent/two_wheeler_wheel_4_try2.jpg`). The geometry then declined it ("water only touches the outline"), so the typed geometry checks are the backstop that kept a wrong number out.
* **Water masks are verified and mostly right**, but SAM leaves out the darker strip of water right at a tyre, so the verified water stops a few pixels short and the geometry finds no contact. The pipeline now segments water in a band just below each accepted wheel on which the verifier saw the waterline, and closes a seam of up to 3 px (x1: 1397 px of water added). A unit test shows this restores contact on a synthetic scene; on the real photos the one wheel it applied to was declined for the outline reason above.
* **Latency and cost.** The scene reading takes 13 to 23 s (Muse Spark Open always reasons first). The agent adds 22 to 87 s when there are wheels to check; checks run in parallel, so the time is set by the longest chain of segment, check and re-check. Whole photos took 13.5 to 110 s and cost USD 0.006 to 0.117 at Baseten's list price for Muse Spark Open (input 1.25, output 4.25 per million tokens). SAM 2.1 loads once, in 5 to 12 s from the local cache. A mask step took 0.8 to 11 s in this run on the M1 GPU: a zoomed crop needs its own image embedding, and the time includes waiting for the one SAM instance while the parallel checks use it.
* **Capture time** is read from EXIF where the original file kept it (c2, c3, c4, x1, x2) and returned as `captured_at`.

## What still fails

* No real photo has produced a wheel measurement yet. The limiting step is the mask, not the geometry: SAM 2.1 hiera-small (kept small for the 8 GB machine) merges tyres with exhaust, fork and mudguard. SAM 3.1, a larger SAM 2.1, or a clean side-on photo of a parked scooter with a measured depth are the next things to try.
* The verifier sometimes accepts a partial outline even with full reasoning (x1 wheel 4). The geometry declined it here, but that is a backstop, not a guarantee.
* The scene model's own depth estimates have no calibration: they are judgements by eye with a sigma set by rule, and their run-to-run spread on the same photo is 5 to 13 cm.
* A photo with wheels to check takes about one to two minutes, almost all of it in Muse Spark Open calls; that is slow for a live demo.

<!-- real-eval:begin -->
Generated by `uv run --extra local python ../eval/real_photos.py` on 2026-10-27 with muse-spark-open (meta-models/muse-spark-open-1026 on Baseten) and sam-2.1-hiera-small on mps (loaded once in 11.7 s). No ground truth exists for these photos; only qualitative behaviour is shown.

### c1: Chennai flood water logging near ofice nandanam4.jpg (named in the brief)

Author Vijayingarsal, licence CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/), source https://commons.wikimedia.org/wiki/File:Chennai_flood_water_logging_near_ofice_nandanam4.jpg.

Result: **model_estimate**, depth 35.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 35.0 cm (sigma 20.0 cm), basis: wading depth at calves and lower wheel submersion in centre street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: wading depth at calves and lower wheel submersion in centre street.

Wall time 17.4 s (timings ms: scene 17290); model cost USD 0.011714.

Overlay: `eval/out/c1_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1632x918 px | water visible; references: person, person; own estimate 35 cm | no wheel: report own estimate | 17290 |
| estimate | photo | muse-spark-open | no measurable wheel | 35 cm, sigma 20 cm | report model estimate | 0 |

### c2: India - Chennai - Monsoon - 04 (3058215637).jpg (named in the brief)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3058215637/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_04_(3058215637).jpg.

Result: **model_estimate**, depth 24.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 24.0 cm (sigma 20.0 cm), basis: water covers whole carriageway and laps halfway up kerb with wave ripples.
Captured at (EXIF): 2008-10-22T10:04:35.

Reasons: standing water visible; two-wheeler wheel 1: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water covers whole carriageway and laps halfway up kerb with wave ripples.

Wall time 46.3 s (timings ms: scene 22625, agent 22334, geometry 0); model cost USD 0.027785.

Overlay: `eval/out/c2_overlay.png`. Verifier images: `eval/out/c2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, kerb; own estimate 24 cm | segment and verify 1 wheel and the water | 22625 |
| segment | water | sam-2.1-hiera-small | box [733,977,1609,1325] px + points [[1122, 1124]] | mask 331898 px (11% of photo) | verify | 4241 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies entirely on the brown floodwater covering the street surface in the foreground. | accept | 10229 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1769,814,1826,927] px + point [1794,879] | mask 4640 px, bbox [1764,808,1830,931] px, score 0.503 | verify | 5006 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1490,562,2048,1177] px with the outline drawn | is_target=false, outline wrong, view head_on, edge visible 0.70, waterline visible; Magenta outline is in flooded water reflection, not the front tyre's narrow upright band above the water. | refine with box [1824,737,1853,822] px + points [1839,742], [1831,782] - points [1809,882], [1815,924] | 10314 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1824,737,1853,822] px + points [1839,742], [1831,782] - points [1809,882], [1815,924] | mask 1218 px, bbox [1827,730,1849,819] px, score 0.754 | verify | 1081 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1710,648,1966,904] px with the outline drawn | is_target=true, outline good, view head_on, edge visible 0.75, waterline visible; Magenta outline follows the narrow head-on front tyre outer edge closely above water without including mudguard or fork. | give up: tyre seen head-on, its outline cannot be measured | 5861 |
| estimate | photo | muse-spark-open | no measurable wheel | 24 cm, sigma 20 cm | report model estimate | 0 |

### c3: Inundation of Chennai in 2017-3.jpg (named in the brief)

Author Kaitha Poo Manam, licence CC BY 3.0 (https://creativecommons.org/licenses/by/3.0/), source https://commons.wikimedia.org/wiki/File:Inundation_of_Chennai_in_2017-3.jpg.

Result: **model_estimate**, depth 105.0 cm, sigma 31.5 cm, confidence 0.23.
Scene model's own estimate: 105.0 cm (sigma 31.5 cm), basis: Water reaches waist to chest of adults walking in centre of flooded street.
Captured at (EXIF): 2015-12-02T11:04:31.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches waist to chest of adults walking in centre of flooded street.

Wall time 17.9 s (timings ms: scene 17383); model cost USD 0.011616.

Overlay: `eval/out/c3_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: person, person, person; own estimate 105 cm | no wheel: report own estimate | 17383 |
| estimate | photo | muse-spark-open | no measurable wheel | 105 cm, sigma 32 cm | report model estimate | 0 |

### c4: Chennai-floods-2015-dec-7.jpg (named in the brief)

Author Destination8infinity, licence CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/), source https://commons.wikimedia.org/wiki/File:Chennai-floods-2015-dec-7.jpg.

Result: **model_estimate**, depth 130.0 cm, sigma 40.0 cm, confidence 0.23.
Scene model's own estimate: 130.0 cm (sigma 40.0 cm), basis: Ground-floor shop shutters submerged to mid-height.
Captured at (EXIF): 2015-12-02T07:56:12.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Ground-floor shop shutters submerged to mid-height.

Wall time 13.5 s (timings ms: scene 13427); model cost USD 0.006326.

Overlay: `eval/out/c4_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 980x735 px | water visible; references: none; own estimate 130 cm | no wheel: report own estimate | 13427 |
| estimate | photo | muse-spark-open | no measurable wheel | 130 cm, sigma 40 cm | report model estimate | 0 |

### x1: India - Chennai - Monsoon - 07 (3059060932).jpg (extra, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059060932/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_07_(3059060932).jpg.

Result: **model_estimate**, depth 14.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 14.0 cm (sigma 20.0 cm), basis: Water reaches lower quarter of motorcycle tyres across flooded street.
Captured at (EXIF): 2008-10-22T10:06:36.

Reasons: standing water visible; two-wheeler wheel 1: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); two-wheeler wheel 2: the scene model did not confirm the mask (Magenta outline encloses the exhaust silencer along with the tyre instead of following tyre outer edge only.); two-wheeler wheel 3: the scene model did not confirm the mask (Magenta outline encloses exhaust pipe and rear suspension in addition to the tyre, not the tyre alone.); two-wheeler wheel: water does not meet the wheel (water only touches the outline); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches lower quarter of motorcycle tyres across flooded street.

Wall time 110.3 s (timings ms: scene 23042, agent 86516, geometry 564); model cost USD 0.117062.

Overlay: `eval/out/x1_overlay.png`. Verifier images: `eval/out/x1_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel; own estimate 14 cm | segment and verify 4 wheels and the water | 23042 |
| segment | water | sam-2.1-hiera-small | box [246,1121,1517,1506] px + points [[778, 1309]] | mask 119385 px (4% of photo) | verify | 7769 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is valid floodwater but only a small foreground patch missing the contiguous water directly around the submerged motorcycles. | refine with box [0,653,1864,1528] px + point [893, 934] | 9367 |
| segment | water | sam-2.1-hiera-small | box [0,653,1864,1528] px + points [[893, 934]] | mask 1276960 px (41% of photo) | verify | 5057 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta mask aligns with the rippled floodwater covering the street and surrounding the parked motorcycles. | accept | 9308 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1354,786,1539,1012] px + point [1444,891] | mask 21811 px, bbox [1349,831,1528,1030] px, score 0.669 | verify | 8604 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [834,298,2048,1518] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Magenta outline extends below waterline to include the wheel's reflection in the floodwater instead of following only the tyre above water. | refine with box [1402,636,1519,813] px + points [1458,654], [1496,732] - points [1456,887], [1452,963] | 11377 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1402,636,1519,813] px + points [1458,654], [1496,732] - points [1456,887], [1452,963] | mask 12176 px, bbox [1398,622,1536,828] px, score 0.772 | verify | 3068 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1199,457,1735,993] px with the outline drawn | is_target=true, outline good, view oblique, edge visible 0.78, waterline visible; Magenta outline follows outer black tyre edge closely above water with rim inside and excludes chrome mudguard and fork, suitable for depth measurement. | accept | 19400 |
| confirm | two wheeler wheel 1 | muse-spark-open | same crop, full reasoning | is_target=false, outline partial, view head_on, edge visible 0.45, waterline visible; Magenta is the front wheel but its top follows over the chrome mudguard/fork that hides the tyre, adding non-tyre bodywork above the visible rubber. | refine with box [1447,654,1537,851] px + points [1484,679], [1519,785] - points [1405,733], [1408,653] | 30704 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1447,654,1537,851] px + points [1484,679], [1519,785] - points [1405,733], [1408,653] | mask 9337 px, bbox [1432,644,1536,835] px, score 0.665 | verify | 1146 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1215,478,1754,1017] px with the outline drawn | is_target=true, outline good, view head_on, edge visible 0.75, waterline visible; Magenta follows the black tyre's outer edge above water on the head-on wheel without including the chrome mudguard or reflection. | give up: tyre seen head-on, its outline cannot be measured | 9770 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-small | box [696,839,976,1027] px + point [829,926] | mask 49627 px, bbox [676,878,1009,1061] px, score 0.6 | verify | 9441 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [10,118,1675,1536] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; magenta outline traces water/reflection beneath bike, not the round black tyre edge above water. | refine with box [584,593,1026,855] px + points [796,600], [609,735] - points [842,940], [909,969] | 13655 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-small | box [584,593,1026,855] px + points [796,600], [609,735] - points [842,940], [909,969] | mask 48327 px, bbox [543,585,1010,883] px, score 0.75 | verify | 771 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [157,106,1412,1362] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.65, waterline visible; Magenta outline strays off tyre onto mudguard/suspension/bodywork and does not follow round outer tyre edge, invalid for water depth. | refine with box [403,602,672,922] px + points [534,637], [439,765] - points [850,690], [577,831] | 34041 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-small | box [403,602,672,922] px + points [534,637], [439,765] - points [850,690], [577,831] | mask 39517 px, bbox [414,611,667,878] px, score 0.546 | verify | 978 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [122,346,954,1178] px with the outline drawn | is_target=false, outline partial, view side, edge visible 0.85, waterline visible; Magenta outline encloses the exhaust silencer along with the tyre instead of following tyre outer edge only. | give up | 7729 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-small | box [438,833,716,1032] px + point [578,934] | mask 55891 px, bbox [420,865,751,1071] px, score 0.734 | verify | 10227 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [0,124,1413,1536] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Magenta outline encloses water/reflection below tyre, not the black rubber tyre above water, so it does not follow tyre outer edge. | refine with box [413,649,781,958] px + points [588,694], [729,809] - points [565,991], [596,951] | 16207 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-small | box [413,649,781,958] px + points [588,694], [729,809] - points [565,991], [596,951] | mask 61730 px, bbox [414,629,818,881] px, score 0.717 | verify | 5733 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [89,267,1142,1320] px with the outline drawn | is_target=false, outline partial, view side, edge visible 0.55, waterline visible; Magenta follows left part of tyre but bleeds right onto suspension/exhaust and neighbouring bike wheel beyond tyre edge | refine with box [414,641,652,912] px + points [539,664], [432,785] - points [597,847], [716,823] | 14759 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-small | box [414,641,652,912] px + points [539,664], [432,785] - points [597,847], [716,823] | mask 38964 px, bbox [414,619,663,879] px, score 0.844 | verify | 3611 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [158,385,919,1146] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.80, waterline visible; Magenta outline encloses exhaust pipe and rear suspension in addition to the tyre, not the tyre alone. | give up | 9809 |
| segment | two wheeler wheel 4 | sam-2.1-hiera-small | box [209,802,423,987] px + point [317,897] | mask 44032 px, bbox [187,790,434,1011] px, score 0.627 | verify | 11028 |
| verify | two wheeler wheel 4 | muse-spark-open | crop [0,283,928,1518] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Magenta outline encloses rippled flood water/reflection, not any tyre rubber or rim, so it cannot measure water depth on a wheel. | refine with box [191,537,401,763] px + points [295,546], [229,625] - points [289,850], [291,792] | 13302 |
| segment | two wheeler wheel 4 | sam-2.1-hiera-small | box [191,537,401,763] px + points [295,546], [229,625] - points [289,850], [291,792] | mask 26576 px, bbox [195,537,381,752] px, score 0.813 | verify | 1045 |
| verify | two wheeler wheel 4 | muse-spark-open | crop [2,356,590,944] px with the outline drawn | is_target=false, outline partial, view side, edge visible 0.65, waterline visible; Magenta outline follows tyre but also encloses exhaust/muffler and inner engine area, not tyre only. | refine with box [193,545,341,756] px + points [264,554], [199,653] - points [338,667], [305,711] | 33541 |
| segment | two wheeler wheel 4 | sam-2.1-hiera-small | box [193,545,341,756] px + points [264,554], [199,653] - points [338,667], [305,711] | mask 17124 px, bbox [194,535,360,753] px, score 0.813 | verify | 776 |
| verify | two wheeler wheel 4 | muse-spark-open | crop [0,358,564,933] px with the outline drawn | is_target=true, outline good, view side, edge visible 0.65, waterline visible; Magenta outline tracks the black tyre outer edge above water with rim inside and does not include mudguard or exhaust. | accept | 6668 |
| confirm | two wheeler wheel 4 | muse-spark-open | same crop, full reasoning | is_target=true, outline good, view side, edge visible 0.68, waterline visible; Magenta outline follows the tyre outer edge above water with rim inside and does not include mudguard, exhaust or water reflection. | accept | 18765 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 1397 px of water added next to the tyres | join to the verified water | 1278 |
| geometry | two wheeler wheel 4 | geometry | wheel and water masks | water does not meet the wheel (water only touches the outline) | rejected | 560 |
| estimate | photo | muse-spark-open | no measurable wheel | 14 cm, sigma 20 cm | report model estimate | 0 |

### x2: India - Chennai - Monsoon - 02 (3059047958).jpg (extra, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059047958/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_02_(3059047958).jpg.

Result: **model_estimate**, depth 23.0 cm, sigma 20.0 cm, confidence 0.22.
Scene model's own estimate: 23.0 cm (sigma 20.0 cm), basis: water reaches lower third of motorcycle wheels standing on flooded road.
Captured at (EXIF): 2008-10-22T09:17:24.

Reasons: standing water visible; two-wheeler wheel 1: the scene model did not confirm the mask (Magenta follows tyre but also encloses exhaust pipe and swingarm/chain area giving wrong depth measurement.); two-wheeler wheel 2: the scene model did not confirm the mask (Magenta outline includes front mudguard, fork and brake disc so it does not follow tyre rubber alone.); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water reaches lower third of motorcycle wheels standing on flooded road.

Wall time 66.5 s (timings ms: scene 15161, agent 51264, geometry 0); model cost USD 0.04331.

Overlay: `eval/out/x2_overlay.png`. Verifier images: `eval/out/x2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 23 cm | segment and verify 2 wheels and the water | 15161 |
| segment | water | sam-2.1-hiera-small | box [246,799,964,1445] px + points [[588, 1083]] | mask 325606 px (10% of photo) | verify | 2250 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but misses the flooded area directly around the motorcycles standing in it. | refine with box [717,922,1188,1352] px + point [922, 1152] | 22107 |
| segment | water | sam-2.1-hiera-small | box [717,922,1188,1352] px + points [[922, 1152]] | mask 264388 px (8% of photo) | verify | 4106 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the brown floodwater surface immediately beside the submerged motorcycle and covers open water. | accept | 22357 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1198,1026,1441,1245] px + point [1313,1106] | mask 21928 px, bbox [1247,998,1478,1134] px, score 0.202 | verify | 3049 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [638,422,2038,1536] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.65, waterline visible; Magenta traces only a small bottom fragment at the waterline instead of the tyre's outer edge above water, missing the main visible arc. | refine with box [1205,856,1506,1057] px + points [1359,862], [1212,962] | 21994 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1205,856,1506,1057] px + points [1359,862], [1212,962] | mask 47658 px, bbox [1215,841,1529,1095] px, score 0.522 | verify | 4361 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [946,547,1788,1389] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; Magenta outline strays far beyond tyre to include exhaust, rear suspension, mudguard and bodywork instead of following tyre outer edge. | refine with box [1245,901,1472,1170] px + points [1354,922], [1270,1048] - points [1401,985], [1245,880] | 11298 |
| segment | two wheeler wheel 1 | sam-2.1-hiera-small | box [1245,901,1472,1170] px + points [1354,922], [1270,1048] - points [1401,985], [1245,880] | mask 30440 px, bbox [1247,904,1498,1134] px, score 0.74 | verify | 892 |
| verify | two wheeler wheel 1 | muse-spark-open | crop [1022,686,1721,1385] px with the outline drawn | is_target=false, outline partial, view side, edge visible 0.75, waterline visible; Magenta follows tyre but also encloses exhaust pipe and swingarm/chain area giving wrong depth measurement. | give up | 9601 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-small | box [1698,1001,1916,1230] px + point [1806,1091] | mask 56803 px, bbox [1681,996,1937,1255] px, score 0.824 | verify | 3806 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [1162,478,2048,1536] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Magenta outline encloses flooded water/pavement beside front wheel and does not follow the tyre's round outer edge. | refine with box [1698,804,1951,1055] px + points [1826,832], [1720,944] - points [1782,1113], [1773,1166] | 21263 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-small | box [1698,804,1951,1055] px + points [1826,832], [1720,944] - points [1782,1113], [1773,1166] | mask 30852 px, bbox [1706,821,1960,1004] px, score 0.675 | verify | 5127 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [1488,589,2048,1270] px with the outline drawn | is_target=false, outline partial, view side, edge visible 0.75, waterline visible; Magenta outline follows the front fender/mudguard on top instead of the black tyre's outer edge, adding bodywork to the tyre. | refine with box [1723,834,1953,1035] px + points [1836,837], [1939,933] - points [1836,822], [1761,856] | 8293 |
| segment | two wheeler wheel 2 | sam-2.1-hiera-small | box [1723,834,1953,1035] px + points [1836,837], [1939,933] - points [1836,822], [1761,856] | mask 21863 px, bbox [1753,823,1960,1001] px, score 0.729 | verify | 1029 |
| verify | two wheeler wheel 2 | muse-spark-open | crop [1533,621,2048,1237] px with the outline drawn | is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Magenta outline includes front mudguard, fork and brake disc so it does not follow tyre rubber alone. | give up | 6849 |
| estimate | photo | muse-spark-open | no measurable wheel | 23 cm, sigma 20 cm | report model estimate | 0 |

<!-- real-eval:end -->
