# Real-photo evaluation

**There is no ground truth for these photos.** Nobody measured the water in them, so nothing here is an accuracy figure. What this shows is qualitative behaviour on real street photos: what the models located, what the verify-and-refine agent accepted or rejected and why, which method answered, and the real latency and cost. Accuracy figures for the geometry come from the synthetic evaluation in `docs/GEOMETRY.md`; field accuracy needs photos with measured depths.

Rerun from `perception/` with `uv run --extra local python ../eval/real_photos.py` (live: Muse Spark Open on Baseten, SAM 2.1 hiera-small locally; add `--modal` for hiera-large on the Modal CPU service). The SAM size comparison is `../eval/sam_ab.py` (table in `RESULTS_SAM_AB.md`) and the latency breakdown is `../eval/latency.py`. The photos are downloaded from Wikimedia Commons into `eval/photos/` and are not in git; their source, author and licence are in `eval/commons_photos.json` and repeated per photo below. Overlays, response JSON and every image the verifier was shown go to `eval/out/` (also not in git). There are two sets. The Unsplash close-ups (`eval/unsplash_photos.json`, Unsplash License, photographers credited there) are the real use case: a resident photographing a vehicle a few metres away. The Commons set (`eval/commons_photos.json`) is the four wide street photos named in the first brief (c1 to c4) plus two with motorcycles standing in water side-on (x1, x2).

## What changed in this milestone

* SAM is prompted with the box **and** a ring of points on the tyre rubber, with negative points on the hub and on what touches the tyre (exhaust, mudguard, wall); the scene model is asked for those points. A box alone let a wall leak into the c3 scooters on the Modal probe.
* All of SAM's proposals (box with points, points alone) are closed into solid wheel shapes (a tyre ring is closed with its convex hull) and ranked by how well the outline fits one ellipse (`wheel_shape.py`). The best two go to Muse Spark Open as two images, A and B; it picks one and judges it.
* An outline is measured only if Muse Spark Open calls it good on a close crop, its shape score is at least 0.35, and its bounding box overlaps the tyre box Muse draws on the same crop by at least 0.7 (IoU).
* Water regions confirmed as water are kept when the agent refines towards more water.
* SAM 2.1 can run on the Modal service (hiera-large, the default when `MODAL_SAM_URL` is set, with local hiera-small as the fallback) or locally (small, base-plus, large, fp32 or fp16).
* Muse Spark Open runs the scene read at reasoning effort `low` (Baseten's lowest for this model; the default is `xhigh`), images go compressed (1024 px at JPEG quality 75; check crops at 512 px), and scene readings are cached by photo, prompt and settings.
* `POST /v1/measure/stream` sends each step as server-sent events, then the result.

## Close-up photos and the vehicle-box wheel search (29 October)

The scene model now boxes each vehicle, and its wheels are searched for inside the lower part of the box without relying on the scene model's points: SAM is prompted from a 6 by 4 grid of points and from boxes around dark, roughly elliptical blobs (OpenCV threshold and ellipse fit), every proposal is ranked by wheel shape times tyre darkness times a size plausible for the vehicle (`wheel_finder.py`), and the best two go to the verifier with the same three-way accept rule. A fourth check was added after the x1 result below: an outline that is darker just outside than just inside (the brightness ratio below 0.8) follows the rim, inside the dark tyre, and is not measured.

Final run, SAM 2.1 hiera-small locally, fresh scene readings, network normal (overlays and every verifier image in `eval/out/final2/`, the generated section below has every step):

| Photo | Result | Estimate by the scene model | Wall time | Model cost USD | Overlay |
|---|---|---|---|---|---|
| Creta, Kolkata (u_aby-GGLtD-A) | no geometry: no tyre visible | 55 cm (sigma 20) | 12 s | 0.008 | `eval/out/final2/u_aby-GGLtD-A_overlay.png` |
| orange utility truck (u_0WlJgFHTHE8) | no geometry: outline takes in the mudguard | 35 cm (sigma 20) | 46 s | 0.035 | `eval/out/final2/u_0WlJgFHTHE8_overlay.png` |
| pickup in a bow wave (u_5T7dG1MRA3U) | no geometry: tyre hidden in splash | 35 cm (sigma 20) | 42 s | 0.032 | `eval/out/final2/u_5T7dG1MRA3U_overlay.png` |
| car under water (u_ple6thPMX3c) | no wheel above water | 65 cm (sigma 20) | 9 s | 0.006 | `eval/out/final2/u_ple6thPMX3c_overlay.png` |
| no wheels (u_Zo6hsIyy75o) | no wheel | 105 cm (sigma 31.5) | 7 s | 0.005 | `eval/out/final2/u_Zo6hsIyy75o_overlay.png` |
| c1 | no geometry | 38 cm (sigma 20) | 42 s | 0.024 | `eval/out/final2/c1_overlay.png` |
| c2 | no geometry | 25 cm (sigma 20) | 29 s | 0.015 | `eval/out/final2/c2_overlay.png` |
| c3 | no wheel | 100 cm (sigma 30) | 27 s | 0.007 | `eval/out/final2/c3_overlay.png` |
| c4 | no wheel | 150 cm (sigma 45) | 7 s | 0.003 | `eval/out/final2/c4_overlay.png` |
| x1 | no geometry (one wheel accepted, declined by the geometry) | 30 cm (sigma 20) | 59 s | 0.070 | `eval/out/final2/x1_overlay.png` |
| x2 | **geometry: 1.5 cm, sigma 3.1 cm** (rear wheel) | 25 cm, reported beside it | 49 s | 0.026 | `eval/out/final2/x2_overlay.png` |

What I saw in the overlays and verifier images:

* **The Creta, the key demo photo, shows no tyre at all.** Both wheel arches are visible, but the water fills them to above the tyres (`eval/out/close-small/u_aby-GGLtD-A_agent/car_1_wheel_try0.jpg` shows the arch openings). No outline method can measure a tyre that is not in the picture; the honest output is the scene model's estimate (55 to 60 cm across runs: "water reaches mid-bumper and lower doors"). Measuring from the wheel arch itself would need the arch's height above the ground for that model, which is a new kind of reference and is not built.
* **The utility truck and the pickup** are now searched as light trucks on car-size wheels (the generic car wheel size, with its wider sigma), when the scene model names a car wheel inside a truck's box. The truck's wheel outlines kept taking in the black mudguard, and the pickup's front wheel is inside its own bow wave; in one run the verifier called a bumper-and-splash outline "good" and the box-agreement check rejected it (`eval/out/close-small/u_5T7dG1MRA3U_agent/truck_1_wheel_try1.jpg`). Trucks and other vehicles without car-size wheels are not measured ("no wheel size is known for a truck").
* **The submerged car and the no-wheel photo** are declined for geometry and answered with the estimate, as intended.
* **The vehicle search finds tyres the scene model's points missed.** On x1 and x2 it ranked full side-on wheels first (wheel scores 0.7 or more); on x1 two of them reached the geometry in an earlier run.
* **A wrong measurement got through once, and is now blocked.** In that earlier run x1 measured 0.6 cm, sigma 1.0 cm: the accepted outline followed the spoked rim inside the tyre (`eval/out/commons-small/x1_agent/two_wheeler_1_wheel_try0.jpg`, panel B), so the fitted wheel was too small and its bottom sat on the waterline. Muse Spark Open called it good and its box agreed (0.82). The outside-over-inside brightness check now rejects such outlines (a unit test covers it), and the final run declined x1.
* **x2's 1.5 cm is plausible but not certain.** The accepted outline follows the rear tyre's outer edge on the left and top and is cut on the right by the exhaust; the fitted waterline is tilted by about 10 degrees, and the wheel size is the generic two-wheeler prior (55 cm, sigma 8.5). By eye the rear tyre's lowest point is just at the water, so a depth of a few centimetres there is believable; the scene model's own 25 cm is for the street as a whole. The sigma (3.1 cm) does not include the tilt, so this number should be read as "shallow at that wheel", not as a calibrated measurement.

SAM size on the close-ups: hiera-small and hiera-large in fp16 reached the same outcome on all five (no geometry; the same rejections), with small faster, so small is the default (`eval/out/close-small/`, `eval/out/close-large16/`). Modal stays selectable (`NEERMATTAM_MASKS=sam2.1-modal`).

Latency with the network normal: photos with no wheel to check answer in 7 to 12 s (one 27 s scene reading); photos with wheels take 29 to 59 s, of which the scene reading is 8 to 12 s and the rest is mask and check rounds. Model cost was USD 0.003 to 0.070 per photo.

## What the earlier runs showed (28 October, Commons set)

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

* One geometry measurement on eleven real photos (x2, 1.5 cm), and it is believable but not certain. The close-up demo photo (the Creta) has no visible tyre, so it cannot be measured from a wheel at all; a wheel-arch reference would be the next step for photos like it.
* Muse Spark Open calls partial, rim-only or bumper-and-splash outlines "good" quite often at low reasoning effort; the numeric checks (shape, box agreement, brightness across the edge) carry the weight, and one of them was only added after a wrong 0.6 cm got through.
* The fitted waterline can tilt where an occluder (an exhaust) cuts the outline, and that tilt is not in the sigma.
* Latency is set by Baseten and the network: 7 to 59 s per photo when both were normal, two to six times that when they were not.
* The scene model's own estimates have no calibration and moved by up to 60 cm between runs on the same photo (c4).

<!-- real-eval:begin -->
Generated by `uv run --extra local python ../eval/real_photos.py` on 2026-10-29 with muse-spark-open (meta-models/muse-spark-open-1026 on Baseten, scene reasoning effort low) and sam-2.1-hiera-small on mps (ready in 6.5 s). No ground truth exists for these photos; only qualitative behaviour is shown.

### u_aby-GGLtD-A: a couple of people standing next to a car in a flooded street (Hyundai Creta, Kolkata) (Unsplash close-up)

Author Dibakar Roy (@dibakar16roy), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/aby-GGLtD-A.

Result: **model_estimate**, depth 55.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 55.0 cm (sigma 20.0 cm), basis: Water reaches mid-bumper and lower doors of SUV and mid-thigh on adult across flooded street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches mid-bumper and lower doors of SUV and mid-thigh on adult across flooded street.

Wall time 12.1 s (timings ms: scene 11955); model cost USD 0.007524.

Overlay: `../eval/out/final2/u_aby-GGLtD-A_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px | water visible; references: person; own estimate 55 cm | no wheel: report own estimate | 11955 |
| estimate | photo | muse-spark-open | no measurable wheel | 55 cm, sigma 20 cm | report model estimate | 0 |

### u_0WlJgFHTHE8: a truck is parked in a flooded street (Unsplash close-up)

Author Juan Manuel Sanchez (@juanman61), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/0WlJgFHTHE8.

Result: **model_estimate**, depth 35.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 35.0 cm (sigma 20.0 cm), basis: Water submerges lower half of truck wheels and dumpsters in foreground..
Captured at (EXIF): not recorded.

Reasons: standing water visible; a light truck on car-size wheels: generic car wheel size used; truck 1 wheel: the scene model did not confirm the mask (Both magenta outlines include the black mudguard/fender and wheel-arch above the tyre instead of following the round black tyre edge, so neither is tyre-only.); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water submerges lower half of truck wheels and dumpsters in foreground..

Wall time 45.8 s (timings ms: scene 8201, agent 37501, geometry 0); model cost USD 0.034827.

Overlay: `../eval/out/final2/u_0WlJgFHTHE8_overlay.png`. Verifier images: `../eval/out/final2/u_0WlJgFHTHE8_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1069 px | water visible; references: car_wheel; own estimate 35 cm | segment and verify 1 wheel and the water | 8201 |
| segment | water | sam-2.1-hiera-small | box [562,694,1574,1056] px + points [[918, 859]] | mask 335459 px (20% of photo) | verify | 1225 |
| segment | truck 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [75,486,829,723] px | 83 proposals from 24 grid points and 4 dark blobs; best: wheel 0.48 = shape 0.51 (fit 0.85, arc 129 deg, ratio 0.68, spill 0.00) x dark 0.89 x size 1.00; wheel 0.23 = shape 0.43 (fit 0.98, arc 81 deg, ratio 0.51, spill 0.00, closed ring) x dark 0.07 x size 1.00 | verify the best two | 8594 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but only foreground, missing water surrounding the truck and bins. | refine with box [832,684,1587,1056] px + point [1200, 871] | 7806 |
| segment | water | sam-2.1-hiera-small | box [832,684,1587,1056] px + points [[1200, 871]] | mask 280188 px (16% of photo) | verify | 345 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta covers only foreground corner and misses the flooded water surrounding the truck and bins. | refine with box [192,663,1520,1048] px + point [544, 845] | 6362 |
| segment | water | sam-2.1-hiera-small | box [192,663,1520,1048] px + points [[544, 845]] | mask 445215 px (26% of photo) | verify | 171 |
| verify | truck 1 wheel | muse-spark-open | crop [294,277,889,872] px with 2 outlines A and B | chose A, shape 0.51 (fit 0.85, arc 129 deg, ratio 0.68, spill 0.00), edge ratio 3.68, box agreement 0.51, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; A is on the tyre while B is on the rear lights, but A's outline rides up onto the orange mudguard/fender instead of following the black tyre's outer edge alone. | refine with box [639,582,719,630] px + points [645,623], [666,592], [699,591], [713,613] - points [679,614], [678,572], [728,577] | 8860 |
| segment | truck 1 wheel | sam-2.1-hiera-small | box [639,582,719,630] px + points [645,623], [666,592], [699,591], [713,613] - points [679,614], [678,572], [728,577] | 6 proposals, 6 distinct; best shapes 0.23, 0.01 | verify the best two | 1181 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta overlay covers the open flooded water surface in the foreground around the orange truck. | accept | 5022 |
| verify | truck 1 wheel | muse-spark-open | crop [499,400,850,751] px with 2 outlines A and B | chose B, shape 0.01 (fit 0.46, arc 201 deg, ratio 0.59, spill 0.19), edge ratio 0.85, box agreement 0.19, is_target=false, outline partial, view side, edge visible 0.65, waterline visible; B follows the tyre arc above water better than A but its upper extension takes in door and bodywork so it is not a clean tyre-only outline. | refine with box [641,600,708,639] px + points [644,627], [665,605], [689,606], [704,625] - points [674,621], [673,558], [669,586] | 8816 |
| segment | truck 1 wheel | sam-2.1-hiera-small | box [641,600,708,639] px + points [644,627], [665,605], [689,606], [704,625] - points [674,621], [673,558], [669,586] | 6 proposals, 6 distinct; best shapes 0.53, 0.46 | verify the best two | 1391 |
| verify | truck 1 wheel | muse-spark-open | crop [548,487,804,743] px with 2 outlines A and B | chose A, shape 0.53 (fit 1.00, arc 96 deg, ratio 0.90, spill 0.00, closed ring), edge ratio 1.18, box agreement 0.46, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; Both magenta outlines include the black mudguard/fender and wheel-arch above the tyre instead of following the round black tyre edge, so neither is tyre-only. | give up | 8508 |
| estimate | photo | muse-spark-open | no measurable wheel | 35 cm, sigma 20 cm | report model estimate | 0 |

### u_5T7dG1MRA3U: white crew cab truck (Downtown, Jacksonville, United States) (Unsplash close-up)

Author Wade Austin Ellis (@wadeaustinellis), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/5T7dG1MRA3U.

Result: **model_estimate**, depth 35.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 35.0 cm (sigma 20.0 cm), basis: Water covers lower portion of pickup tyre on flooded roadway.
Captured at (EXIF): not recorded.

Reasons: standing water visible; a light truck on car-size wheels: generic car wheel size used; truck 1 wheel: the scene model did not confirm the mask (outline and the tyre's box disagree); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers lower portion of pickup tyre on flooded roadway.

Wall time 42.4 s (timings ms: scene 7844, agent 34443, geometry 0); model cost USD 0.031691.

Overlay: `../eval/out/final2/u_5T7dG1MRA3U_overlay.png`. Verifier images: `../eval/out/final2/u_5T7dG1MRA3U_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1143 px | water visible; references: car_wheel; own estimate 35 cm | segment and verify 1 wheel and the water | 7844 |
| segment | water | sam-2.1-hiera-small | box [6,11,673,1134] px + points [[275, 561]] | mask 750444 px (41% of photo) | verify | 1162 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta region is water but on left side only and misses the flooded water immediately surrounding the truck. | refine with box [656,709,1574,1138] px + point [1096, 966] | 6456 |
| segment | truck 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [612,258,1600,647] px | 84 proposals from 24 grid points and 4 dark blobs; best: wheel 0.41 = shape 0.55 (fit 1.00, arc 99 deg, ratio 0.48, spill 0.00, closed ring) x dark 0.50 x size 1.00; wheel 0.37 = shape 0.47 (fit 0.79, arc 135 deg, ratio 0.55, spill 0.00, closed ring) x dark 0.59 x size 1.00 | verify the best two | 7691 |
| segment | water | sam-2.1-hiera-small | box [656,709,1574,1138] px + points [[1096, 966]] | mask 326274 px (18% of photo) | verify | 354 |
| verify | truck 1 wheel | muse-spark-open | crop [522,0,1600,1064] px with 2 outlines A and B | chose A, shape 0.55 (fit 1.00, arc 99 deg, ratio 0.48, spill 0.00, closed ring), edge ratio 1.05, box agreement 0.25, is_target=false, outline wrong, view side, edge visible 0.45, waterline visible; Neither outline follows tyre rubber; A traces the blue mudguard/fender above tyre, B is on water splash below door, both miss true tyre edge | refine with box [894,489,1066,633] px + points [912,553], [959,505], [1029,516], [1052,564] - points [980,585], [969,463], [1147,654] | 7593 |
| segment | truck 1 wheel | sam-2.1-hiera-small | box [894,489,1066,633] px + points [912,553], [959,505], [1029,516], [1052,564] - points [980,585], [969,463], [1147,654] | 6 proposals, 6 distinct; best shapes 0.54, 0.11 | verify the best two | 1251 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region is floodwater surface and is part of the continuous water body surrounding the truck | accept | 8661 |
| verify | truck 1 wheel | muse-spark-open | crop [673,228,1329,884] px with 2 outlines A and B | chose B, shape 0.11 (fit 0.41, arc 126 deg, ratio 0.57, spill 0.01), edge ratio 1.88, box agreement 0.64, is_target=true, outline good, view oblique, edge visible 0.60, waterline visible; B tightly follows the tyre's outer edge above the water while A strays onto the bow wave and bodywork | refine with box [882,490,1039,599] px + points [891,558], [925,511], [989,506], [1027,541] - points [960,555], [960,476], [956,635] | 7829 |
| segment | truck 1 wheel | sam-2.1-hiera-small | box [882,490,1039,599] px + points [891,558], [925,511], [989,506], [1027,541] - points [960,555], [960,476], [956,635] | 6 proposals, 6 distinct; best shapes 0.55, 0.37 | verify the best two | 1307 |
| verify | truck 1 wheel | muse-spark-open | crop [359,22,1548,1143] px with 2 outlines A and B | chose B, shape 0.37 (fit 0.92, arc 150 deg, ratio 0.63, spill 0.10, closed ring), edge ratio 0.37, box agreement 0.06, is_target=true, outline good, view oblique, edge visible 0.60, waterline visible; B traces the tyre's outer rubber edge above the water while A encloses a large splash area. | give up: the outline covers a different area than the tyre (0.06) | 8591 |
| estimate | photo | muse-spark-open | no measurable wheel | 35 cm, sigma 20 cm | report model estimate | 0 |

### u_ple6thPMX3c: A car is submerged in floodwater (Putney Embankment, London, UK) (Unsplash close-up)

Author John Cameron (@john_cameron), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/ple6thPMX3c.

Result: **model_estimate**, depth 65.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 65.0 cm (sigma 20.0 cm), basis: Water reaches mid-doors and base of trees with car half-submerged on flooded riverside road..
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches mid-doors and base of trees with car half-submerged on flooded riverside road..

Wall time 9.4 s (timings ms: scene 9365); model cost USD 0.00558.

Overlay: `../eval/out/final2/u_ple6thPMX3c_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1536x2048 px | water visible; references: none; own estimate 65 cm | no wheel: report own estimate | 9365 |
| estimate | photo | muse-spark-open | no measurable wheel | 65 cm, sigma 20 cm | report model estimate | 0 |

### u_Zo6hsIyy75o: a van is in a flooded area with trees in the background (Unsplash close-up)

Author Caspian Dahlström (@caspian_dahlstrom), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/Zo6hsIyy75o.

Result: **model_estimate**, depth 105.0 cm, sigma 31.5 cm, confidence 0.22.
Scene model's own estimate: 105.0 cm (sigma 31.5 cm), basis: Water reaches mid-doors/windows of pickup and lower walls of caravans indicating ~1m depth.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches mid-doors/windows of pickup and lower walls of caravans indicating ~1m depth.

Wall time 6.7 s (timings ms: scene 6625); model cost USD 0.004685.

Overlay: `../eval/out/final2/u_Zo6hsIyy75o_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px | water visible; references: none; own estimate 105 cm | no wheel: report own estimate | 6625 |
| estimate | photo | muse-spark-open | no measurable wheel | 105 cm, sigma 32 cm | report model estimate | 0 |

### c1: Chennai flood water logging near ofice nandanam4.jpg (Commons, named in the brief)

Author Vijayingarsal, licence CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/), source https://commons.wikimedia.org/wiki/File:Chennai_flood_water_logging_near_ofice_nandanam4.jpg.

Result: **model_estimate**, depth 38.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 38.0 cm (sigma 20.0 cm), basis: water reaches mid wheel and knees of pedestrians across street centre.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (A is only splash water, B is the only outline on the front tyre but it is too wide and includes fork/mudguard.); two wheeler 2 wheel: the scene model did not confirm the mask (no wheel-like proposal in the vehicle box); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water reaches mid wheel and knees of pedestrians across street centre.

Wall time 42.1 s (timings ms: scene 9470, agent 32536, geometry 0); model cost USD 0.024387.

Overlay: `../eval/out/final2/c1_overlay.png`. Verifier images: `../eval/out/final2/c1_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1632x918 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel, person, person; own estimate 38 cm | search 2 vehicle boxes for wheels, check 2 wheels and segment the water | 9470 |
| segment | water | sam-2.1-hiera-small | box [331,617,810,815] px + points [[627, 708]] | mask 107819 px (7% of photo) | verify | 1258 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1018,445,1064,478] px | 74 proposals from 24 grid points and 1 dark blobs; none wheel-like | give up | 5498 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [830,469,914,519] px | 75 proposals from 24 grid points and 1 dark blobs; best: wheel 0.28 = shape 0.28 (fit 1.00, arc 51 deg, ratio 0.85, spill 0.00, closed ring) x dark 0.96 x size 1.00; wheel 0.17 = shape 0.29 (fit 1.00, arc 54 deg, ratio 0.46, spill 0.01) x dark 0.19 x size 1.00 | verify the best two | 5880 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta box lies on the continuous brown floodwater in the foreground where people wade, correctly marking the water surface. | accept | 6652 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [683,292,1068,677] px with 2 outlines A and B | chose A, shape 0.28 (fit 1.00, arc 51 deg, ratio 0.85, spill 0.00, closed ring), edge ratio 1.00, box agreement 0.72, is_target=true, outline good, view side, edge visible 0.55, waterline visible; A follows the front wheel tyre outer arc above the water while B is detached in the water and does not trace any tyre. | refine with box [837,469,858,500] px + points [837,486], [847,471], [857,483] - points [848,484], [847,464], [864,496] | 5746 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [837,469,858,500] px + points [837,486], [847,471], [857,483] - points [848,484], [847,464], [864,496] | 6 proposals, 6 distinct; best shapes 0.25, 0.00 | verify the best two | 1412 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [717,357,973,613] px with 2 outlines A and B | chose A, shape 0.25 (fit 1.00, arc 48 deg, ratio 0.70, spill 0.01, closed ring), edge ratio 1.53, box agreement 0.51, is_target=false, outline wrong, view side, edge visible 0.35, waterline visible; A is larger and closer to wheel position than B's tiny sliver but it encloses red bodywork/leg and mudguard not just tyre rubber, so it fails strict tyre-only t | refine with box [840,467,870,505] px + points [843,494], [851,473], [863,475], [867,491] - points [855,487], [851,480], [837,494] | 9237 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [840,467,870,505] px + points [843,494], [851,473], [863,475], [867,491] - points [855,487], [851,480], [837,494] | 6 proposals, 6 distinct; best shapes 0.55, 0.01 | verify the best two | 1341 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [736,364,992,620] px with 2 outlines A and B | chose B, shape 0.01 (fit 1.00, arc 78 deg, ratio 0.61, spill 0.20, closed ring), edge ratio 0.91, box agreement 0.56, is_target=false, outline partial, view side, edge visible 0.62, waterline visible; A is only splash water, B is the only outline on the front tyre but it is too wide and includes fork/mudguard. | give up | 8775 |
| estimate | photo | muse-spark-open | no measurable wheel | 38 cm, sigma 20 cm | report model estimate | 0 |

### c2: India - Chennai - Monsoon - 04 (3058215637).jpg (Commons, named in the brief)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3058215637/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_04_(3058215637).jpg.

Result: **model_estimate**, depth 25.0 cm, sigma 20.0 cm, confidence 0.22.
Scene model's own estimate: 25.0 cm (sigma 20.0 cm), basis: Open road flooded with water reaching lower tyres and kerb top, consistent across centre of street..
Captured at (EXIF): 2008-10-22T10:04:35.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Open road flooded with water reaching lower tyres and kerb top, consistent across centre of street..

Wall time 29.4 s (timings ms: scene 12051, agent 17268, geometry 0); model cost USD 0.015373.

Overlay: `../eval/out/final2/c2_overlay.png`. Verifier images: `../eval/out/final2/c2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: kerb, two_wheeler_wheel; own estimate 25 cm | search 1 vehicle box for wheels, check 1 wheel and segment the water | 12051 |
| segment | water | sam-2.1-hiera-small | box [684,955,1699,1471] px + points [[1100, 1247]] | mask 507545 px (16% of photo) | verify | 1282 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only foreground and misses flooded area around the motorcyclist and waders. | refine with box [717,783,1393,1014] px + point [1085, 860] | 6277 |
| segment | water | sam-2.1-hiera-small | box [717,783,1393,1014] px + points [[1085, 860]] | mask 139597 px (4% of photo) | verify | 624 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1787,760,1896,910] px | 81 proposals from 24 grid points and 3 dark blobs; best: wheel 0.34 = shape 0.42 (fit 0.72, arc 165 deg, ratio 0.47, spill 0.02) x dark 0.60 x size 1.00; wheel 0.23 = shape 0.25 (fit 1.00, arc 45 deg, ratio 0.79, spill 0.00, closed ring) x dark 0.84 x size 1.00 | verify the best two | 8804 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the flooded street surface surrounding the motorcyclist. | accept | 5171 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [1726,687,1982,943] px with 2 outlines A and B | chose B, shape 0.25 (fit 1.00, arc 45 deg, ratio 0.79, spill 0.00, closed ring), edge ratio 0.93, box agreement 0.10, is_target=true, outline good, view head_on, edge visible 0.85, waterline visible; B follows the front tyre outer edge above water with expected flat waterline while A outlines water beside the bike | give up: tyre seen head-on, its outline cannot be measured | 8365 |
| estimate | photo | muse-spark-open | no measurable wheel | 25 cm, sigma 20 cm | report model estimate | 0 |

### c3: Inundation of Chennai in 2017-3.jpg (Commons, named in the brief)

Author Kaitha Poo Manam, licence CC BY 3.0 (https://creativecommons.org/licenses/by/3.0/), source https://commons.wikimedia.org/wiki/File:Inundation_of_Chennai_in_2017-3.jpg.

Result: **model_estimate**, depth 100.0 cm, sigma 30.0 cm, confidence 0.22.
Scene model's own estimate: 100.0 cm (sigma 30.0 cm), basis: water reaches waists to chests of adults across entire street with vehicles submerged to seat level.
Captured at (EXIF): 2015-12-02T11:04:31.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water reaches waists to chests of adults across entire street with vehicles submerged to seat level.

Wall time 26.5 s (timings ms: scene 26416); model cost USD 0.006775.

Overlay: `../eval/out/final2/c3_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: person, person, person, person; own estimate 100 cm | no wheel: report own estimate | 26416 |
| estimate | photo | muse-spark-open | no measurable wheel | 100 cm, sigma 30 cm | report model estimate | 0 |

### c4: Chennai-floods-2015-dec-7.jpg (Commons, named in the brief)

Author Destination8infinity, licence CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/), source https://commons.wikimedia.org/wiki/File:Chennai-floods-2015-dec-7.jpg.

Result: **model_estimate**, depth 150.0 cm, sigma 45.0 cm, confidence 0.21.
Scene model's own estimate: 150.0 cm (sigma 45.0 cm), basis: Water submerges ground floors and shop fronts up to awning level on the right.
Captured at (EXIF): 2015-12-02T07:56:12.

Reasons: standing water visible; no wheel found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water submerges ground floors and shop fronts up to awning level on the right.

Wall time 7.4 s (timings ms: scene 7405); model cost USD 0.002566.

Overlay: `../eval/out/final2/c4_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 980x735 px | water visible; references: none; own estimate 150 cm | no wheel: report own estimate | 7405 |
| estimate | photo | muse-spark-open | no measurable wheel | 150 cm, sigma 45 cm | report model estimate | 0 |

### x1: India - Chennai - Monsoon - 07 (3059060932).jpg (Commons, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059060932/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_07_(3059060932).jpg.

Result: **model_estimate**, depth 30.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 30.0 cm (sigma 20.0 cm), basis: Water covers lower half of motorcycle wheels and fully covers road surface across foreground.
Captured at (EXIF): 2008-10-22T10:06:36.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (outline only partly right: A traces only a narrow edge fragment while B traces the whole tyre above water albeit loosely wavy); two wheeler 2 wheel: the scene model did not confirm the mask (outline accepted by the scene model but not wheel-shaped); two-wheeler wheel 3: the scene model did not confirm the mask (outline and the tyre's box disagree); two-wheeler wheel: water does not meet the wheel; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers lower half of motorcycle wheels and fully covers road surface across foreground.

Wall time 58.9 s (timings ms: scene 10840, agent 47876, geometry 27); model cost USD 0.069786.

Overlay: `../eval/out/final2/x1_overlay.png`. Verifier images: `../eval/out/final2/x1_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel; own estimate 30 cm | search 4 vehicle boxes for wheels, check 5 wheels and segment the water | 10840 |
| segment | water | sam-2.1-hiera-small | box [16,1001,1859,1534] px + points [[877, 1355]] | mask 818252 px (26% of photo) | verify | 1365 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-small | box [238,707,478,986] px + points [258,799], [334,740], [414,788], [446,862] - points [348,854], [221,897], [492,831] | 6 proposals, 6 distinct; best shapes 0.85, 0.37 | verify the best two | 4069 |
| segment | two wheeler wheel 4 | sam-2.1-hiera-small | box [1810,578,2022,1096] px + points [1833,717], [1896,616], [1970,707], [1999,840] - points [1919,854], [1802,725], [2038,842] | 6 proposals, 6 distinct; best shapes 0.48, 0.36 | verify the best two | 4441 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [0,178,921,1345] px with 2 outlines A and B | chose B, shape 0.37 (fit 0.76, arc 117 deg, ratio 0.94, spill 0.00, closed ring), edge ratio 2.37, box agreement 0.63, is_target=true, outline good, view side, edge visible 0.55, waterline visible; B follows the round outer edge of the rear motorcycle tyre above the water while A is a small splash artifact; B is tight to the tyre without including mudguard | refine with box [191,542,391,822] px + points [201,682], [286,565], [370,688] - points [293,696], [290,526], [428,740] | 6301 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but in foreground only and misses the water directly around the motorcycles and people. | refine with box [16,799,1812,1518] px + point [840, 1152] | 10154 |
| verify | two wheeler wheel 4 | muse-spark-open | crop [1180,66,2048,1536] px with 2 outlines A and B | chose A, shape 0.48 (fit 0.99, arc 123 deg, ratio 0.58, spill 0.06, closed ring), edge ratio 1.10, box agreement 0.92, is_target=true, outline good, view side, edge visible 0.65, waterline visible; A follows the outer black tyre arc above the water while B is a small sliver missing the tyre, so A is the correct wheel outline for water depth. | accept | 7158 |
| segment | water | sam-2.1-hiera-small | box [16,799,1812,1518] px + points [[840, 1152]] | mask 1207600 px (38% of photo) | verify | 1531 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-small | box [191,542,391,822] px + points [201,682], [286,565], [370,688] - points [293,696], [290,526], [428,740] | 6 proposals, 6 distinct; best shapes 0.48, 0.27 | verify the best two | 2903 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [612,615,1637,1109] px | 84 proposals from 24 grid points and 4 dark blobs; best: wheel 0.51 = shape 0.54 (fit 0.97, arc 141 deg, ratio 0.45, spill 0.05, closed ring) x dark 0.87 x size 1.00; wheel 0.39 = shape 0.40 (fit 0.73, arc 144 deg, ratio 0.69, spill 0.01, closed ring) x dark 0.96 x size 1.00 | verify the best two | 15088 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [429,601,1125,1102] px | 83 proposals from 24 grid points and 4 dark blobs; best: wheel 0.45 = shape 0.50 (fit 0.97, arc 96 deg, ratio 0.78, spill 0.00, closed ring) x dark 0.79 x size 1.00; wheel 0.44 = shape 0.47 (fit 0.81, arc 129 deg, ratio 0.71, spill 0.00, closed ring) x dark 0.87 x size 1.00 | verify the best two | 16792 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta overlay aligns with the flooded foreground water surface around the motorcycles. | accept | 5289 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [0,316,650,1047] px with 2 outlines A and B | chose A, shape 0.48 (fit 0.84, arc 123 deg, ratio 0.79, spill 0.00, closed ring), edge ratio 2.16, box agreement 0.67, is_target=true, outline good, view side, edge visible 0.88, waterline visible; A follows the round black tyre outer edge above the water with rim inside while B is only a thin sliver on the sidewall. | refine with box [204,554,390,797] px + points [211,706], [255,592], [340,587], [382,690] - points [294,698], [348,714], [237,568] | 8492 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [75,169,1238,1331] px with 2 outlines A and B | chose B, shape 0.47 (fit 0.81, arc 129 deg, ratio 0.71, spill 0.00, closed ring), edge ratio 0.77, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B follows the round outer edge of the middle bike rear tyre above water while A is only the hub, so B is the correct tyre outline for depth measurement | refine with box [798,766,880,853] px + points [448,744], [523,609], [615,652], [632,752] - points [538,786], [533,607], [603,806] | 6571 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-small | box [204,554,390,797] px + points [211,706], [255,592], [340,587], [382,690] - points [294,698], [348,714], [237,568] | 6 proposals, 6 distinct; best shapes 0.69, 0.45 | verify the best two | 1665 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [233,9,1773,1536] px with 2 outlines A and B | chose B, shape 0.40 (fit 0.73, arc 144 deg, ratio 0.69, spill 0.01, closed ring), edge ratio 1.11, box agreement 0.42, is_target=true, outline good, view side, edge visible 0.75, waterline visible; B traces the rear tyre's outer edge above the water while A marks engine bodywork, so B is the correct tyre outline. | refine with box [726,612,1011,872] px + points [749,757], [849,627], [972,696], [995,788] - points [857,742], [857,620], [872,841] | 8969 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [798,766,880,853] px + points [448,744], [523,609], [615,652], [632,752] - points [538,786], [533,607], [603,806] | 6 proposals, 6 distinct; best shapes 0.33, 0.00 | verify the best two | 1828 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [726,612,1011,872] px + points [749,757], [849,627], [972,696], [995,788] - points [857,742], [857,620], [872,841] | 6 proposals, 6 distinct; best shapes 0.65, 0.59 | verify the best two | 1667 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [0,315,676,1033] px with 2 outlines A and B | chose A, shape 0.69 (fit 1.00, arc 126 deg, ratio 0.96, spill 0.00, closed ring), edge ratio 0.87, box agreement 0.53, is_target=true, outline good, view side, edge visible 0.85, waterline visible; A follows the black tyre outer edge tightly above the straight waterline while B cuts inside the tyre and misses most of the rubber | give up: the outline covers a different area than the tyre (0.53) | 7911 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [467,349,1253,1135] px with 2 outlines A and B | chose A, shape 0.65 (fit 0.98, arc 120 deg, ratio 0.54, spill 0.00, closed ring), edge ratio 1.67, box agreement 0.29, is_target=true, outline good, view side, edge visible 0.65, waterline visible; Outline A follows the rear tyre outer edge above the water with rim inside and straight waterline below, B is only a thin sliver of rubber. | refine with box [711,704,976,878] px + points [722,822], [791,729], [909,730], [957,813] - points [843,819], [728,673], [791,657] | 8385 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [576,538,1036,999] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.85, waterline visible; A's magenta outline is inside the wheel around the hub and B's mark is on the number plate, neither follows the tyre's outer edge above the water. | refine with box [707,669,921,881] px + points [806,686], [729,762], [897,765], [735,864] - points [827,818], [877,700], [670,758] | 9898 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [711,704,976,878] px + points [722,822], [791,729], [909,730], [957,813] - points [843,819], [728,673], [791,657] | 6 proposals, 6 distinct; best shapes 0.47, 0.36 | verify the best two | 2209 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [707,669,921,881] px + points [806,686], [729,762], [897,765], [735,864] - points [827,818], [877,700], [670,758] | 6 proposals, 6 distinct; best shapes 0.32, 0.28 | verify the best two | 1683 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [525,482,1102,1060] px with 2 outlines A and B | chose A, shape 0.32 (fit 0.58, arc 186 deg, ratio 0.59, spill 0.01), edge ratio 2.12, box agreement 0.80, is_target=true, outline good, view side, edge visible 0.70, waterline visible; A closely follows the round outer black tyre above the water while B cuts diagonally across the wheel as a triangle. | give up: the outline does not fit a wheel (shape 0.32 (fit 0.58, arc 186 deg, ratio 0.59, spill 0.01)) | 7960 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [461,379,1251,1169] px with 2 outlines A and B | chose B, shape 0.36 (fit 0.60, arc 201 deg, ratio 0.94, spill 0.00), edge ratio 2.00, box agreement 0.79, is_target=true, outline partial, view side, edge visible 0.85, waterline visible; A traces only a narrow edge fragment while B traces the whole tyre above water albeit loosely wavy | give up: outline only partly right | 10044 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 36863 px of water added next to the tyres | join to the verified water | 1149 |
| geometry | two wheeler wheel 4 | geometry | wheel and water masks | water does not meet the wheel | rejected | 25 |
| estimate | photo | muse-spark-open | no measurable wheel | 30 cm, sigma 20 cm | report model estimate | 0 |

### x2: India - Chennai - Monsoon - 02 (3059047958).jpg (Commons, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059047958/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_02_(3059047958).jpg.

Result: **wheel_geometry**, depth 1.5 cm, sigma 3.1 cm, confidence 0.36.
Scene model's own estimate: 16.0 cm (sigma 20.0 cm), basis: Water covers road up to lower tyre on motorcycle, shallow sheet flow in centre.
Captured at (EXIF): 2008-10-22T09:17:24.

Reasons: standing water visible; two-wheeler wheel found; wheel size unknown, generic prior used; scene model waterline agrees within 2.9 cm.

Wall time 48.5 s (timings ms: scene 8404, agent 38731, geometry 1155); model cost USD 0.026426.

Overlay: `../eval/out/final2/x2_overlay.png`. Verifier images: `../eval/out/final2/x2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 16 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 8404 |
| segment | water | sam-2.1-hiera-small | box [90,459,2018,1534] px + points [[537, 934]] | mask 1607363 px (51% of photo) | verify | 1007 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region accurately covers the flooded street water surrounding the partially submerged motorcycle. | accept | 5649 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1154,859,1942,1309] px | 84 proposals from 24 grid points and 4 dark blobs; best: wheel 0.56 = shape 0.64 (fit 0.94, arc 129 deg, ratio 0.78, spill 0.00, closed ring) x dark 0.74 x size 1.00; wheel 0.47 = shape 0.51 (fit 0.82, arc 138 deg, ratio 0.86, spill 0.00, closed ring) x dark 0.84 x size 1.00 | verify the best two | 7932 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [808,297,2048,1536] px with 2 outlines A and B | chose B, shape 0.51 (fit 0.82, arc 138 deg, ratio 0.86, spill 0.00, closed ring), edge ratio 1.97, box agreement 0.48, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B outline follows rear tyre outer edge above straight waterline, while A outline is a tiny patch on front wheel not the tyre perimeter. | refine with box [1248,960,1465,1201] px + points [1267,1090], [1335,972], [1428,1084], [1447,1140] - points [1360,1109], [1316,916], [1453,1214] | 7670 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1248,960,1465,1201] px + points [1267,1090], [1335,972], [1428,1084], [1447,1140] - points [1360,1109], [1316,916], [1453,1214] | 6 proposals, 6 distinct; best shapes 0.67, 0.62 | verify the best two | 1624 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [1023,719,1720,1415] px with 2 outlines A and B | chose A, shape 0.67 (fit 0.98, arc 126 deg, ratio 0.81, spill 0.00, closed ring), edge ratio 1.12, box agreement 0.65, is_target=true, outline good, view side, edge visible 0.75, waterline visible; A follows the outer tyre edge above water while B is a tiny water splash, so A is the correct side-view tyre outline for water-depth measurement. | refine with box [1252,914,1426,1157] px + points [1261,1043], [1293,949], [1392,952], [1413,1029] - points [1344,1057], [1312,890], [1403,987] | 9298 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1252,914,1426,1157] px + points [1261,1043], [1293,949], [1392,952], [1413,1029] - points [1344,1057], [1312,890], [1403,987] | 6 proposals, 6 distinct; best shapes 0.86, 0.70 | verify the best two | 1396 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [1023,703,1679,1359] px with 2 outlines A and B | chose A, shape 0.86 (fit 0.93, arc 189 deg, ratio 0.79, spill 0.00, closed ring), edge ratio 1.97, box agreement 0.81, is_target=true, outline good, view side, edge visible 0.68, waterline visible; Outline A follows the round outer black tyre edge above water closely without including mudguard or exhaust, B strays slightly at the top edge | accept | 9418 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 668 px of water added next to the tyres | join to the verified water | 1127 |
| geometry | two wheeler wheel 1 | geometry | wheel and water masks | depth 1.5 cm, sigma 1.0 cm, 273 degrees of rim visible | candidate | 1154 |
| estimate | photo | geometry | measurement 1.5 cm and model estimate 16 cm | difference 14 cm, two combined sigma 40 cm | use the measurement | 0 |

<!-- real-eval:end -->
