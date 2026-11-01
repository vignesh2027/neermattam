# Real-photo evaluation

**There is no ground truth for these photos.** Nobody measured the water in them, so nothing here is an accuracy figure. What this shows is qualitative behaviour on real street photos: what the models located, what the verify-and-refine agent accepted or rejected and why, which method answered, and the real latency and cost. Accuracy figures for the geometry come from the synthetic evaluation in `docs/GEOMETRY.md`; field accuracy needs photos with measured depths.

Rerun from `perception/` with `uv run --extra local python ../eval/real_photos.py` (live: Muse Spark Open on Baseten, SAM 2.1 hiera-small locally; add `--modal` for hiera-large on the Modal CPU service). The SAM size comparison is `../eval/sam_ab.py` (table in `RESULTS_SAM_AB.md`) and the latency breakdown is `../eval/latency.py`. The photos are downloaded from Wikimedia Commons into `eval/photos/` and are not in git; their source, author and licence are in `eval/commons_photos.json` and repeated per photo below. Overlays, response JSON and every image the verifier was shown go to `eval/out/` (also not in git). There are two sets. The Unsplash close-ups (`eval/unsplash_photos.json`, Unsplash License, photographers credited there) are the real use case: a resident photographing a vehicle a few metres away. The Commons set (`eval/commons_photos.json`) is the four wide street photos named in the first brief (c1 to c4) plus two with motorcycles standing in water side-on (x1, x2).

**The current result is the generated section at the end of this file** (run of 2026-10-30, Muse Spark Open on Baseten and SAM 2.1 hiera-small locally). Of the eleven photos, none was measured from a wheel; two were measured from a person (c1 45.6 cm, sigma 13.0 cm, confidence 0.22; c3 102.9 cm, sigma 10.9 cm, confidence 0.28); the other nine got the scene model's labelled estimate (confidence 0.21 to 0.23). Wall time was 7.2 to 50.2 s per photo. A wheel-focused set of 22 more photos was added on 1 November (section "Photos chosen for the wheel method", and its own generated block at the very end): one of them measured from a wheel reproducibly, and only with a fixed scene reading. The hand-written sections below describe other runs, each with its date or output folder, and their numbers differ from the generated section because the scene reading is fresh on each run and the verifier is not deterministic.

## Photos chosen for the wheel method (1 November)

**Result: one real photo measured from a wheel with an outline on the tyre and a believable depth, and only while the scene reading stayed the same; three were needed.** w_kanchipuram (a parked motorcycle on a Kanchipuram market street, McKay Savage, CC BY 2.0) gave 4.5 cm from its rear wheel in all 6 runs that reused one scene reading, sigma 4.3 to 6.0 cm, two of them under the service's own 55 s budget. With a fresh scene reading it was not measured in 4 runs out of 4. Two other photos published a wheel depth in the full run, u_A-yqsrAME6g (23.5 cm) and p_21960691 (25.4 cm, the same in 4 of 4 runs), but from outlines that do not follow the tyre's outer edge, so I do not count them. Nobody measured the water in any of these photos, so nothing here is accuracy. "By eye" is my own reading of the photo, not a measurement.

**What was collected.** 22 photos of vehicles standing in shallow to moderate water with a tyre seen from the side, listed in `eval/wheel_photos.json` with author, licence, source page and what I saw when I chose each one: Wikimedia Commons (8: four more of McKay Savage's 2008 Chennai monsoon series and his Kanchipuram photo, CC BY 2.0; a Kerala police Tata Sumo and a Borivali state bus, CC BY-SA 2.0; mopeds in Quang Ngai, CC BY-SA 3.0; a hatchback in Iskenderun, CC BY 3.0), Unsplash (7, Unsplash License: six by Aldward Castillo, one by Dibakar Roy) and Pexels (7, Pexels License, all by Dibakar Roy in Kolkata). The photos are downloaded into `eval/photos/` and stay out of git.

**Runs.** SAM 2.1 hiera-small on this laptop, Muse Spark Open on Baseten at scene effort `low`. After the first run the scene readings came from a cache (`--scene-cache ../eval/out/scene-cache-wheel`), so later runs differ only in the checks, as a second upload of the same photo to the service would:

| Output folder | Code | Settings | What it shows |
|---|---|---|---|
| `eval/out/wheel-base` | main at 6b62807 | 55 s budget, 20 s per check, as the service | no wheel check finished on 20 photos (slow uplink, below) |
| `eval/out/wheel-nob`, `wheel-v2`, `wheel-v3`, `wheel-v4` | main, then this branch as it grew | no budget, 20 s per check | diagnosis; many checks still timed out |
| `eval/out/wheel-final` | this branch at cbb7408 | no budget, 120 s per check | the generated block at the end of this file; no check timed out |
| `eval/out/wheel-rep1` to `rep3` | same | same | three repeats of the three photos that measured |
| `eval/out/wheel-svc1`, `svc2` | same | 55 s budget, 20 s per check | w_kanchipuram under the service's settings |
| `eval/out/wheel-fresh1`, `2`, `wheel-freshnob1`, `2` | same | fresh scene readings, with and without the budget | w_kanchipuram with a new scene reading each time |

The full run is `uv run --extra local python ../eval/real_photos.py --sets wheel --sam-size small --budget 0 --verify-timeout 120 --marker wheel-eval --scene-cache ../eval/out/scene-cache-wheel --out ../eval/out/wheel-final`, from `perception/`.

**The full run** (`wheel-final`, generated block below): 3 photos answered by a wheel, 1 by a person, 18 by the scene model's labelled estimate. 34 wheels were searched for or checked, 8 outlines were accepted and 6 reached the geometry: 3 published, 3 set aside by the shallow-side guard (u_ic8V3A59gpg 2.7 cm against an estimate of 22 cm at that wheel; u_P7Z3HwNWPeQ 2.5 cm, which was a bicycle's wheel that the scene model called a two-wheeler's, against 26 cm; w_kerala4x4 15.3 cm, an outline taking in the mudguard and with its bottom on a bow wave, against 32 cm), and 2 were declined by the geometry (w_chennai01's moped, 99 degrees of rim visible; w_borivali_bus, water does not meet the wheel). Wall time 22 to 157 s per photo (median 61 s) without a budget; Baseten cost USD 0.62 for the run.

**w_kanchipuram, the one that held.** The outline follows the rear tyre's outer edge from the left round the bottom to the top; at the upper right the shock absorber crosses the wheel and the outline cuts across it in a straight line, which the geometry drops as an occluder (195 degrees of rim visible). The overlay's fitted circle sits on the tyre. Depth 4.5 cm with the generic two-wheeler size (55 cm, sigma 8.5; the scene model did not name the motorcycle's model); the scene model's second waterline agreed within 0.1 to 4.2 cm. The scene model's own estimate for the street is 12 cm, and it puts the water at the ankle (about 6 cm) of the man near the motorcycle; by eye the water just covers the bottom of the tyre. Depth plus 1.5 sigma is 11 to 14 cm, under the 15 cm car limit, so this reading would leave the street open to cars, which by eye is right for this street. A person in the same photo read 27 cm and was not used. Repeats with the same scene reading: 4.5, 4.5, 4.5, 4.5 cm (no budget) and 4.5, 4.5 cm (55 s budget, 50 and 34 s); the outline was the same each time, so the spread is 0.0 cm and the sigma varied (4.3 to 6.0) with the second waterline. With a fresh scene reading the wheel was not measured: twice the budget ran out (the scene reading took 19 and 24 s over this uplink), once the verifier called the same outline "partial" and the refinements gave rim outlines, and once the outline and the tyre box disagreed. So this measurement is reproducible only as long as the scene reading is, and I would not show it as a live first upload.

**The two that published from wrong outlines.** p_21960691 (a Maruti Swift in Kolkata, black and white): 25.4 cm in 4 of 4 runs against the scene model's 27 cm, but the outline covers the rim and the right half of the rear tyre and misses its left side, and the fitted ellipse is narrower than the tyre. u_A-yqsrAME6g (a motorcycle ridden through water): 23.5 cm against 19 cm in the full run, from an outline that takes in the swingarm and shock absorber; in the repeats it was declined twice and measured 8.8 cm once, which the guard set aside. Both are on the deep side of the estimates, so they are not the dangerous kind of error, but they are not measurements of the tyre either.

**Why outlines miss the tyre's outer edge.** From every verifier image and from all of SAM's proposals for the same prompts, rendered offline:

* *Two-wheelers:* the mudguard hugs the top of the tyre and SAM puts them in one mask whatever the prompt; a ring of points on the rubber with negative points just outside the tyre box (tried offline on u_w8MXURXvqjQ, w_chennai03, p_23939154) moved the outline onto the tyre on the left and top but kept the fork and mudguard on the right. The fork, brake disc, shock absorber and swingarm split the visible tyre, so the convex hull of the rubber is not the wheel either.
* *Cars:* the wheel arch above the tyre is a dark, round, tyre-sized void; on p_26202090's front wheel SAM's best-ranked proposal (shape 0.92, darkness 0.97) is the arch. On a dark car the tyre has no edge against the body (u_ic8V3A59gpg) and the outline settles on the rim and the inner rubber.
* *The verifier at reasoning effort `low`* calls such outlines good (u_w8MXURXvqjQ's front wheel with its mudguard, accepted in three runs, measured 15.2 and 12.2 cm). At full reasoning it rejected two of three that I had judged wrong but accepted p_23939154's, and each such check took 25 to 45 s and USD 0.018 to 0.033, too slow for the 55 s budget.
* *The scene model's estimates run deep on shallow water,* and a correct shallow reading is then set aside. On p_26202090 it says 38 cm for the street and 42 cm at the rear wheel, where by eye the water is a little below the hub of a 62 cm tyre (about 25 to 30 cm); a correct reading there with a 4 cm sigma would not be published. That is the guard doing its job, and it limits the wheel path to photos where the estimate is not inflated.
* *The scene model has no bicycle and no truck:* it called a bicycle's wheel a two-wheeler's (u_P7Z3HwNWPeQ, caught by the guard) and a lorry's tyre a bus's (w_chennai01, published at 3.6 cm by main's code, fixed below).

**The uplink of this laptop.** On the evening of 1 November a text-only call to the same model answered in 1.6 and 1.9 s while an image check (one or two 512 px JPEGs, 60 to 100 KB) took 10 to 45 s, and uploading 200 KB to an unrelated host ran at 2.3 to 29 KB/s; at one point another process on this machine was sending about 45 KB/s. With checks cut at 20 s, many failed, and when the water check failed the photo had no verified water: four wheels the agent had accepted were lost this way (w_kanchipuram, p_23939154, p_21960691 and u_lIkOxcck448 in the diagnosis runs). The deployed service calls Baseten from a data centre, so this measures the evaluation machine, not the service; the full run therefore cut each check at 120 s instead.

**What I changed** (each with tests in `perception/tests/`):

1. *A truck's tyre is not measured as a bus wheel* (`pipeline.truck_wheels_dropped`): a bus or auto-rickshaw wheel inside a truck's box is dropped with the existing reason "no wheel size is known for a truck", unless it also lies in a bus or auto box. This removed the only wrong reading that was published (w_chennai01, 3.6 cm against the 106 cm bus tyre).
2. *The scene model's own wheels are prompted in the vehicle-box search* (`agent.find_vehicle_wheel`, `wheel_finder.grow_region`). They used to be thrown away; on the close-up u_ic8V3A59gpg the tyre lay above the lower part of the car's box and the verifier was shown a fog light. Now the tyre is found and checked (and set aside by the guard, above).
3. *A proposal four times larger than its tyre box is not shown to the verifier* (`agent.spills_far_beyond`): on p_26202090 a box around the tyre gave the whole side of the car as panel B.
4. *A wheel found in a vehicle's box takes the position and estimate of the scene model's wheel it overlaps* (`pipeline.matching_hint`), so front and rear tyres of a catalogue two-wheeler are not mixed, and the guard compares with the estimate at that wheel.
5. *Water confirmed by an earlier check is kept when a later water check fails* (`agent.refine_water`), as it already was when the budget ran out.
6. *Evaluation:* `real_photos.py` has the new set (`--sets wheel`), its own generated block (`--marker wheel-eval`), and diagnosis flags `--budget 0` and `--verify-timeout`; Unsplash downloads fall back to `curl -4` when the HTTP client gets a 403.

None of the safety rules changed: the shallow-side guard, the brightness check across the edge, unknown is never safe and the labelling are as they were. Changes 1 and 3 only remove candidates; 2, 4 and 5 add prompts or keep evidence the model already confirmed. In the runs above the guard set aside every wheel reading that was clearly too shallow.

**Cost.** Baseten spend for everything in this section was about USD 2.10: USD 1.99 summed over the runs' `cost_usd`, plus single-image probes and the full-reasoning comparison.

**What it would take.** Three things stood between this branch and three demo photos: outlines that follow the tyre's outer edge on two-wheelers (the mudguard) and dark cars (the arch and body); a verifier that does not call those outlines good at low reasoning effort; and a scene reading whose estimate is not inflated on shallow water. A full-reasoning confirmation of only the outline about to be measured caught two of three bad outlines here and is the cheapest next step to try; a tyre outline grown from the rim, which SAM finds reliably, outwards to the dark rubber followed the wheel arch on p_26202090 in a first offline try and is not ready.

## What changed in this milestone

* SAM is prompted with the box **and** a ring of points on the tyre rubber, with negative points on the hub and on what touches the tyre (exhaust, mudguard, wall); the scene model is asked for those points. A box alone let a wall leak into the c3 scooters on the Modal probe.
* All of SAM's proposals (box with points, points alone) are closed into solid wheel shapes (a tyre ring is closed with its convex hull) and ranked by how well the outline fits one ellipse (`wheel_shape.py`). The best two go to Muse Spark Open as two images, A and B; it picks one and judges it.
* An outline is measured only if Muse Spark Open calls it good on a close crop, its shape score is at least 0.35, and its bounding box overlaps the tyre box Muse draws on the same crop by at least 0.7 (IoU).
* Water regions confirmed as water are kept when the agent refines towards more water.
* SAM 2.1 can run on the Modal service (hiera-large, the default when `MODAL_SAM_URL` is set, with local hiera-small as the fallback) or locally (small, base-plus, large, fp32 or fp16).
* Muse Spark Open runs the scene read at reasoning effort `low` (Baseten's lowest for this model; the default is `xhigh`), images go compressed (1024 px at JPEG quality 75; check crops at 512 px), and scene readings are cached by photo, prompt and settings.
* `POST /v1/measure/stream` sends each step as server-sent events, then the result.

## Rim checks, the shallow-side guard and latency (30 October, later)

**The x1 failure.** The person run of 30 October (next section) accepted an outline of the moped's rear wheel that the scene model called good, whose box agreed with the tyre box (0.86) and whose brightness ratio passed (0.85 against 0.8), and measured 0.7 cm where the scene model said 20 cm. Looking at the proposals again (SAM 2.1 hiera-small, the vehicle-box search, cached scene reading), that outline follows the tyre's left edge and top and then cuts diagonally through the wheel: half a tyre closed by a chord. Its fitted ellipse is an oval (axis ratio 0.57) tilted 38 degrees from upright; the good full-wheel outline of the bike beside it is tilted 1 degree. The outlines that follow the rim instead show a darker ring just outside them (dark share 0.69 outside against 0.15 inside, and 0.42 against 0.05), while tyre outlines show the opposite or no difference.

Two levels of fix:

* **Numeric tyre checks** (`wheel_finder.tyre_failure`, used by the agent and in the vehicle-search ranking), each able to overrule a "good" from the scene model: a darker ring just outside the outline (the rubber is outside, so the outline is on the rim); an oval tilted more than 25 degrees from both image axes with axis ratio below 0.75 (a cut outline; real wheels stand upright and camera roll is small); a diameter outside the range expected for the vehicle box (two-wheeler 18 to 70 percent of the box width, car 8 to 45, auto 9 to 55, bus 4 to 30; wide, to catch a hub or rim fragment and not a slightly small tyre); and the scene model's own points on the tyre rubber, of which at least half must not lie outside the outline. The brightness threshold of 0.8 was left as it was.
* **A shallow-side guard** in the pipeline. The literal rule "more than two combined sigma apart" would not have caught x1: the estimate's sigma is at least 20 cm, so two combined sigma is about 40 cm and the 19 cm gap passes. So the rule is one-sided: when the scene model's estimate lies above the measurement's own two-sigma upper bound, and by more than half the estimate's sigma (10 cm at least), the wheel reading is not published; the estimate is returned as `model_estimate` and `reasons` gives the wheel's reading. I chose falling back over widening the sigma because the routing engine caps sigma at 10 cm (`engine/src/routing.rs`), so a widened sigma would not raise the depth it routes on, and fusion would still pull the cell shallow. A test uses the x1 numbers (0.7 cm, sigma 2.5, estimate 20 cm).

In the run on all eleven photos after these changes (`eval/out/final3`, table below) the numeric checks flagged 11 of the outlines the scene model was shown, on 7 photos, and the guard set aside two wheel readings: x1 (0.5 cm, sigma 9.0, against the scene model's 22 cm at that wheel) and x2 (1.3 cm, sigma 5.2, against 25 cm). x2's reading had looked plausible in earlier runs (the rear tyre's lowest point is close to the water), so this is the cautious side costing a measurement that may have been right; no wheel measurement survived in that run. In the generated run at the end of this file the guard did not act: no wheel outline reached the geometry (on x1 and x2 the checked tyres were seen head-on, and on x1 the time budget stopped one open check).

**Latency.** The 262 to 462 s photos in the person run (next section) were Baseten's tail: single checks of 76 to 160 s where most take 6 to 11 s. Profile of x1 with the network normal (fresh scene reading): scene reading 10.7 s; then, in parallel per object, SAM proposals (1 to 3 s each, but the vehicle-box searches took 5 to 24 s because they wait for each other on the one SAM instance and ranked 50 to 80 full-frame masks each), and three rounds of checks of 6 to 11 s each; geometry 1 to 2 s. What changed:

* every check by Muse Spark Open is cut after 20 s and asked once more (the scene reading after 45 s), instead of waiting up to 120 s per attempt;
* a time budget: no check is started or waited for later than 50 s after the request began (55 s budget, 5 s kept to finish), and the answer then says the budget ran out and is the best reached in time, with a `budget` step in the trace;
* the vehicle-box search ranks its masks on windows around them instead of the full 3-megapixel frame (same scores; ranking of one vehicle's 84 proposals went from 9.4 s to 3.3 s, de-duplication from 3.4 s to 0.1 s);
* smaller images were measured and not adopted: a check took 7.3 and 6.9 s at 1024 px (140 KB) against 6.7 and 9.7 s at 640 px (65 KB), and a scene reading 9.6 s at 1024 px against 10.8 s at 768 px; with the network normal the time follows the length of the model's reasoning, not the upload;
* the person path already runs only when the scene model reports people; the checks of different objects already ran in parallel, and the rounds for one object depend on each other.

Before and after, the same three photos with fresh scene readings, network normal both times:

| Photo | Before | After |
|---|---|---|
| Creta (u_aby-GGLtD-A) | 45.5 s | 39.4 s |
| c2 | 46.3 s | 29.9 s |
| x1 | 46.5 s | 48.8 s (the budget stopped one open check at 48 s) |

Under normal service the gain is modest (the critical path is the scene reading plus up to three rounds of checks); what the change buys is a ceiling: no photo can now take the 262 to 462 s seen in the person run.

Run on all eleven after these changes, SAM 2.1 hiera-small locally, fresh scene readings (`--out ../eval/out/final3`). This table is kept as context and is not the current result; the generated section at the end of this file is:

| Photo | Answer | Scene model's estimate | Wall time | Scene reading | Checks | Cost USD |
|---|---|---|---|---|---|---|
| Creta (u_aby-GGLtD-A) | model_estimate 65 cm, sigma 20 (the man pushing the car declined: no shoulder line) | 65 | 43.8 s | 10.7 s | 31.1 s | 0.035 |
| utility truck (u_0WlJgFHTHE8) | model_estimate 40, sigma 20 | 40 | 38.7 s | 8.8 s | 29.9 s | 0.026 |
| pickup (u_5T7dG1MRA3U) | model_estimate 45, sigma 20 | 45 | 41.5 s | 9.1 s | 32.3 s | 0.030 |
| submerged car (u_ple6thPMX3c) | model_estimate 75, sigma 22.5 | 75 | 7.7 s | 7.6 s | none | 0.003 |
| no wheels (u_Zo6hsIyy75o) | model_estimate 140, sigma 42 | 140 | 8.0 s | 8.0 s | none | 0.006 |
| c1 | **person_reference 54.2 cm, sigma 12.4** (second person 46.7 cm; landmarks mid-thigh and knee) | 30 | 49.8 s | 9.9 s | 34.3 s | 0.051 |
| c2 | model_estimate 25, sigma 20 | 25 | 23.3 s | 9.5 s | 13.6 s | 0.018 |
| c3 | **person_reference 97.0 cm, sigma 12.0** (the man in red; landmark waist, 95 cm) | 105 | 28.1 s | 8.0 s | 16.3 s | 0.011 |
| c4 | model_estimate 120, sigma 36 | 120 | 31.0 s | 9.0 s | 21.9 s | 0.017 |
| x1 | model_estimate 18, sigma 20 (wheel read 0.5 cm, set aside by the guard) | 18 | 51.4 s | 10.7 s | 38.8 s | 0.060 |
| x2 | model_estimate 25, sigma 20 (wheel read 1.3 cm, set aside by the guard) | 25 | 46.3 s | 8.3 s | 36.7 s | 0.027 |

In that run every photo answered in under 52 s (median 38.7 s). The person results moved between runs because the scene reading was fresh: in c3 a different man was measured this time (97 cm, by eye the water is at his hips or waist), and in c1 the left man read 54 cm against 49 cm before. Both are inside their sigma of each other, which is what a sigma of 12 cm means.

## People as the reference (30 October)

The service now also measures against an adult standing in the water (`method: person_reference`, `perception/src/neermattam_perception/person.py`, method and sources in `docs/GEOMETRY.md` section 9). SAM outlines the person from the scene model's box, body point and head top; the depth is `H (1 - 0.182 L / s)`, with `L` the visible length, `s` the head-to-shoulder length, and `H` the NCD-RisC adult stature prior for India (158.75 cm, sigma 9.0 cm, sex unknown). The scene model's body landmark at the waterline (knee, waist and so on) is a cross-check. A wheel is preferred when both are measured. Every head the scene model or SAM finds is blurred before an image leaves the service.

Run on all eleven photos with SAM 2.1 hiera-small locally and fresh scene readings (the scene prompt changed: it now asks for posture, head top, landmark and every face), from `perception/`:

    uv run --extra local python ../eval/real_photos.py --sets close commons --sam-size small \
        --scene-cache ../eval/out/scene-cache --out ../eval/out/person --no-write-results

c1 and the Creta were run a second time with `--only` after one last change to the order of the decline checks; their rows are from that run. Overlays and response JSON are in `eval/out/person/`.

| Photo | People the scene model boxed | Person measurements | Final answer | Scene model's own estimate | Landmark cross-check |
|---|---|---|---|---|---|
| Creta, Kolkata (u_aby-GGLtD-A) | 1 | 0 of 1: no shoulder line (he leans over the car he is pushing; the scene model called him standing) | model_estimate 60 cm, sigma 20 | 60 cm | mid-thigh |
| utility truck (u_0WlJgFHTHE8) | 0 | none | model_estimate 40 cm, sigma 20 | 40 cm | |
| pickup in a bow wave (u_5T7dG1MRA3U) | 0 (driver inside) | none | model_estimate 35 cm, sigma 20 | 35 cm | |
| car under water (u_ple6thPMX3c) | 0 | none | model_estimate 75 cm, sigma 22.5 | 75 cm | |
| no wheels (u_Zo6hsIyy75o) | 0 | none | model_estimate 90 cm, sigma 27 | 90 cm | |
| c1 | 3 | 2 of 3: 49.0 cm (sigma 12.7) and 32.2 cm (sigma 15.2); the third is riding a scooter (posture sitting), declined | **person_reference 49.0 cm, sigma 12.7**, confidence 0.23 | 38 cm | knee, 45 cm (4 cm from the measurement) |
| c2 | 0 | none | model_estimate 28 cm, sigma 20 | 28 cm | |
| c3 | 4 (3 measured at most) | 1 of 3: 72.4 cm (sigma 14.2); one outline was a fragment between two men (box check), one had no shoulder line | **person_reference 72.4 cm, sigma 14.2**, confidence 0.16 | 110 cm | waist, 95 cm (23 cm apart) |
| c4 | 0 (people on a distant rooftop) | none | model_estimate 180 cm, sigma 54 | 180 cm | |
| x1 | 0 | none | wheel_geometry 0.7 cm, sigma 2.5 (wrong; see the section above) | 20 cm | |
| x2 | 0 | none | model_estimate 22 cm, sigma 20 | 22 cm | |

What I saw in the overlays:

* **c1, left man (49 cm).** The outline follows him from head to where his legs enter the water; the shoulder band sits at his shoulders. His legs show to about the knee, and the scene model independently says knee (45 cm). This is the most believable person measurement in the set. The man in the blue shirt gave 32 cm in this run; in the run before, his outline was declined because it stopped short of his box (the verified water mask differs from run to run). By eye the water is between his shin and knee, so 32 cm, sigma 15, is plausible.
* **c3, man in the black jacket (72 cm).** His outline ends where his jacket meets the water, and the shoulder band is on his shoulders. The scene model says waist (95 cm) and its own street estimate is 110 cm. The difference to the landmark is under two sigma, so the sigma was not widened, but the confidence is low (0.16). By eye the water is at his hips; 72 cm is low if he is of average height. I would read this as "about hip deep" and not trust the centimetres.
* **A wrong person measurement was caught and blocked.** In the run before the box check, c3 gave 101.8 cm from an outline that was only a fragment between two men (a head-sized blob), with "water" segmented below it that was really a striped shirt. The outline now has to span at least 60 percent of the scene model's box and end near its bottom, and it was declined in the final run. In a debug run on c3, another outline joined two neighbours' heads into one; two heads above the shoulders are now declined as well.
* **The Creta.** The man pushing the car is the obvious reference, but he leans forward over the bonnet, his outline includes his reflection, and there is no clear shoulder line; he is declined, which is right. The scene model called his posture "standing", so the posture flag alone would not have stopped him.
* **Faces.** In every overlay I looked at (all eleven), the heads of the people in the water, the Creta's driver behind the windscreen, the pickup's driver, the people on c2's pavement and the crowd on c4's rooftop are pixelated. Very small, distant figures that the scene model did not box (a few pixels tall, behind the utility truck and far down c2's street) are not blurred. The service never stores or returns an unblurred overlay; the photo itself still goes to Muse Spark Open and SAM.

Caveats:

* There is no ground truth. Nothing here says the person method is accurate on real photos; it says it produces plausible numbers on two of eleven photos and declines the rest for stated reasons.
* The synthetic check (`docs/GEOMETRY.md` section 9.7: 400 figures, mean absolute error 6.5 cm, 4.4 cm low on average) uses the proportions the method assumes; real bodies, clothes, posture and camera angle add error that only field photos with measured depths can show. Expect plus or minus 10 to 15 cm.
* Where the shoulder is taken is the largest single assumption. Loose clothes, raised arms and hair move it, and the band between the first wide row and full shoulder width is carried in the sigma rather than resolved.
* Results move between runs, mostly because the verified water mask changes (it decides whether a person's legs reach water and how the outline is trimmed): the c1 blue-shirt man was measured in one run and declined in the next.
* **x1 regressed on the wheel path** in this run (0.7 cm from an outline cut through the wheel). That is fixed, and explained, in the section above.
* c1 took 462 s in the full run and 41 s in the rerun; the Creta 75 s and 107 s; the submerged car 262 s. The slow ones were waiting on Baseten during the verification calls.

## Close-up photos and the vehicle-box wheel search (29 October)

The scene model now boxes each vehicle, and its wheels are searched for inside the lower part of the box without relying on the scene model's points: SAM is prompted from a 6 by 4 grid of points and from boxes around dark, roughly elliptical blobs (OpenCV threshold and ellipse fit), every proposal is ranked by wheel shape times tyre darkness times a size plausible for the vehicle (`wheel_finder.py`), and the best two go to the verifier with the same three-way accept rule. A fourth check was added after the x1 result below: an outline that is darker just outside than just inside (the brightness ratio below 0.8) follows the rim, inside the dark tyre, and is not measured.

Final run, SAM 2.1 hiera-small locally, fresh scene readings, network normal (overlays and every verifier image in `eval/out/final2/`; the generated section at the end of this file is a different run, of 30 October):

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
* **x2's 1.5 cm is plausible but not certain.** (The shallow-side guard added on 30 October now sets a reading like this aside; see the first section.) The accepted outline follows the rear tyre's outer edge on the left and top and is cut on the right by the exhaust; the fitted waterline is tilted by about 10 degrees, and the wheel size is the generic two-wheeler prior (55 cm, sigma 8.5). By eye the rear tyre's lowest point is just at the water, so a depth of a few centimetres there is believable; the scene model's own 25 cm is for the street as a whole. The sigma (3.1 cm) does not include the tilt, so this number should be read as "shallow at that wheel", not as a calibrated measurement.

SAM size on the close-ups: hiera-small and hiera-large in fp16 reached the same outcome on all five (no geometry; the same rejections), with small faster, so small is the default (`eval/out/close-small/`, `eval/out/close-large16/`). Modal stays selectable (`NEERMATTAM_MASKS=sam2.1-modal`).

Latency with the network normal: photos with no wheel to check answer in 7 to 12 s (one 27 s scene reading); photos with wheels take 29 to 59 s, of which the scene reading is 8 to 12 s and the rest is mask and check rounds. Model cost was USD 0.003 to 0.070 per photo.

## What the earlier runs showed (28 October, Commons set)

These notes come from looking at the overlays and verifier images of this run, of the five arms of the SAM comparison, and of the latency runs.

* **No wheel measurement passes the judges on these six photos.** Every final answer is the scene model's labelled estimate (`method: model_estimate`, sigma 20 to 42 cm): c1 45 cm, c2 25 cm, c3 95 cm, c4 140 cm, x1 23 cm, x2 22 cm. By eye these are plausible (c1 people about knee deep, c3 a man chest deep, x1 and x2 the lower quarter to third of the tyres under water). Across runs the same photo's estimate moved: c3 between 90 and 125 cm, c4 between 120 and 180 cm, the others by 5 to 15 cm. The stated sigma (30 percent) covers most of that, not all of it.
* **Why the wheels fail.** On the side-on motorcycles of x1 and x2 the proposals either take in the exhaust, swingarm, fork or mudguard (SAM treats the metal attached to the wheel as part of it) or follow only the near half of the tyre. With a hand-placed ring of points on the front tyre of x2, hiera-large did give a full-wheel outline (plus the mudguard), so the masks can be right; the prompts Muse Spark Open gives are not yet precise enough to get there. Its boxes on the full photo usually land on the reflection below a tyre (about 85 px low on a 2048 px photo), and on crops its tyre boxes and points still miss by tens of pixels.
* **The judges stop wrong numbers.** Muse Spark Open alone called partial outlines "good" many times (for example `eval/out/ab-large/x2_agent/two_wheeler_wheel_2_try2.jpg`, panel B covers only the left half of the rear tyre); the box-agreement check rejected them. Before that check existed, a development run measured x2 at 2.8 and 3.5 cm from half-tyre outlines, which is clearly too shallow; those numbers never reached a result in the final code.
* **Head-on tyres** (c2's oncoming motorcycle, one bike in x1) are recognised and dropped at once.
* **Water masks** are accepted on every photo with wheels. The confirmed-water union now keeps the first verified region when the agent refines towards more water (in an earlier c2 run the refinement had replaced a 16 percent mask with a 2 percent one).

## SAM 2.1 sizes (28 October, from `RESULTS_SAM_AB.md`)

Same scene readings for every arm, 10 wheels checked per arm. Every arm accepted the same 1 wheel (the scooter cut off by c2's left edge, whose bottom is outside the photo, so the geometry correctly declined it), except Modal, which accepted none; no arm reached a geometry depth. Medians of per-photo time were 41 s (small), 53 s (base-plus), 57 s (large), 33 s (large fp16) and 79 s (large on Modal CPU), with the scene reading cached. Peak memory: 0.4 to 0.5 GB of process memory and 2.3 to 2.8 GB on the GPU for fp32, 1.5 GB GPU for large in fp16 (1.3 GB process peak while converting). Base-plus pushed this 8 GB machine into swap once (270 s for one photo). On these photos the larger models did not change the outcome, so the size is not the limiting factor; the prompts are.

## Latency

Before this milestone (27 October run, scene effort `xhigh`, 1280 px images): c1 17 s, c2 46 s, c3 18 s, c4 14 s, x1 110 s, x2 67 s.

After, measured on 28 October with `latency.py` (local SAM small, fresh scene readings): c3 11 s and c4 15 s in one run each, but most photos took 120 to 370 s, because Baseten answered image requests very slowly during those runs: the probe call with one 140 to 210 KB image and a one-line answer took 50 to 107 s for most photos, against 4 to 12 s for the others and 6 s earlier in the day. A text-only probe stayed at 1 to 2 s, and an upload of a similar file to another host ran at 44 KB/s, so the delay was on Baseten's side. Under normal service the critical path is the scene reading (8 to 11 s at effort `low`, against 23 s at `xhigh` for the same photo) plus one round of mask (1 to 3 s locally, 12 to 23 s on Modal CPU) and check (6 to 12 s) per refinement. A photo whose scene has no wheel answers in about 10 to 15 s; a photo with one or two wheels took 30 to 52 s with local SAM when the service was normal and the scene reading was cached (8 to 11 s more for a fresh reading), so **the 30 s target is met only when the first proposal is accepted**, and not with SAM on the Modal CPU deployment (80 to 370 s), which needs the GPU deploy.

## What still fails

* No wheel measurement on the eleven real photos in the current run (the generated section below): two were measured from a person (c1 45.6 cm, c3 102.9 cm) and nine got the scene model's labelled estimate. Two wheel readings reached a result in earlier runs: x2 at 1.5 cm (29 October), believable but not certain, and x1 at 0.7 cm (the person run of 30 October), which was wrong. The numeric tyre checks and the shallow-side guard added on 30 October now stop readings like these. The close-up demo photo (the Creta) has no visible tyre, so it cannot be measured from a wheel at all; a wheel-arch reference would be the next step for photos like it.
* Muse Spark Open calls partial, rim-only or bumper-and-splash outlines "good" quite often at low reasoning effort; the numeric checks (shape, box agreement, brightness across the edge) carry the weight, and one of them was only added after a wrong 0.6 cm got through.
* The fitted waterline can tilt where an occluder (an exhaust) cuts the outline, and that tilt is not in the sigma.
* Latency is set by Baseten and the network: 7.2 to 50.2 s per photo in the current run. Before the 55 s time budget, photos took 7 to 59 s when both were normal (29 October) and up to 462 s when Baseten was slow; the budget now stops the open checks instead, so a slow service costs checks rather than time.
* The scene model's own estimates have no calibration and moved by up to 60 cm between runs on the same photo (c4).

<!-- real-eval:begin -->
Generated by `uv run --extra local python ../eval/real_photos.py` on 2026-10-30 with muse-spark-open (meta-models/muse-spark-open-1026 on Baseten, scene reasoning effort low) and sam-2.1-hiera-small on mps (ready in 8.3 s). No ground truth exists for these photos; only qualitative behaviour is shown.

### u_aby-GGLtD-A: a couple of people standing next to a car in a flooded street (Hyundai Creta, Kolkata) (Unsplash close-up)

Author Dibakar Roy (@dibakar16roy), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/aby-GGLtD-A.

Result: **model_estimate**, depth 58.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 58.0 cm (sigma 20.0 cm), basis: Water midway up car wheels and mid-thigh on adult across flooded street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car 1 wheel: the scene model did not confirm the mask (No wheel/tyre is visible in this crop; both magenta outlines trace the front grille and bumper, not any tyre.); person 1: the shoulder line could not be found; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water midway up car wheels and mid-thigh on adult across flooded street.

Wall time 46.1 s (timings ms: scene 12382, agent 31295, geometry 0); model cost USD 0.025153.

Overlay: `eval/out/u_aby-GGLtD-A_overlay.png`. Verifier images: `eval/out/u_aby-GGLtD-A_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px | water visible; references: person, car_wheel, car_wheel; own estimate 58 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 12382 |
| segment | water | sam-2.1-hiera-small | box [1018,558,1600,906] px + points [[1261, 760]] | mask 153006 px (9% of photo) | verify | 1294 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [209,573,1296,969] px | 81 proposals from 24 grid points and 3 dark blobs; best: wheel 0.54 = shape 0.74 (fit 0.93, arc 156 deg, ratio 0.43, spill 0.00, closed ring) x dark 0.44 x size 1.00; wheel 0.47 = shape 0.80 (fit 1.00, arc 144 deg, ratio 0.31, spill 0.00, closed ring) x dark 0.17 x size 1.00 | verify the best two | 5627 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta tint lies entirely on the flooded street water immediately in front of the submerged car. | accept | 11630 |
| verify | car 1 wheel | muse-spark-open | crop [0,0,1600,1067] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; No tyre rubber visible above water; A outlines grille and B outlines water splash, neither follows tyre outer edge. | refine with box [933,721,1233,846] px - points [1075,784], [342,814], [1083,825] | 8386 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [933,721,1233,846] px - points [1075,784], [342,814], [1083,825] | 3 proposals, 3 distinct; best shapes 0.29, 0.27 | verify the best two | 1390 |
| verify | car 1 wheel | muse-spark-open | crop [684,373,1493,1067] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.00, waterline not visible; Both magenta outlines trace the front grille/chrome around the Hyundai badge, not any wheel/tyre which is fully submerged below water and not visible. | refine with box [933,721,1233,846] px - points [1105,760], [966,729], [1093,664] | 6266 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [933,721,1233,846] px - points [1105,760], [966,729], [1093,664] | 3 proposals, 3 distinct; best shapes 0.55, 0.30 | verify the best two | 843 |
| verify | car 1 wheel | muse-spark-open | crop [692,388,1475,1067] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; No wheel/tyre is visible in this crop; both magenta outlines trace the front grille and bumper, not any tyre. | give up | 8617 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points | 1 masks | measure each against the stature prior | 1399 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people the verified water does not reach | 1 water mask | check that each outline ends at water | 906 |
| geometry | person 1 | geometry | person mask | the shoulder line could not be found | declined | 1 |
| estimate | photo | muse-spark-open | no measurable wheel | 58 cm, sigma 20 cm | report model estimate | 0 |

### u_0WlJgFHTHE8: a truck is parked in a flooded street (Unsplash close-up)

Author Juan Manuel Sanchez (@juanman61), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/0WlJgFHTHE8.

Result: **model_estimate**, depth 35.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 35.0 cm (sigma 20.0 cm), basis: water covers road to lower tyre and base of dumpsters with reflections.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel size is known for a truck; bus wheel 1: the scene model did not confirm the mask (outline and the tyre's box disagree); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water covers road to lower tyre and base of dumpsters with reflections.

Wall time 38.8 s (timings ms: scene 7848, agent 30867, geometry 0); model cost USD 0.029798.

Overlay: `eval/out/u_0WlJgFHTHE8_overlay.png`. Verifier images: `eval/out/u_0WlJgFHTHE8_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1069 px | water visible; references: bus_wheel; own estimate 35 cm | segment and verify 1 wheel and the water | 7848 |
| segment | water | sam-2.1-hiera-small | box [499,733,1364,1051] px + points [[925, 905]] | mask 203792 px (12% of photo) | verify | 1633 |
| segment | bus wheel 1 | sam-2.1-hiera-small | box [611,683,691,741] px + points [624,703], [648,690], [672,708] - points [650,712], [650,724] | 6 proposals, 6 distinct; best shapes 0.14, 0.11 | verify the best two | 2927 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but only foreground patch, missing water directly around orange truck and bins. | refine with box [512,663,1408,994] px + point [896, 845] | 8798 |
| verify | bus wheel 1 | muse-spark-open | crop [220,290,1106,1069] px with 2 outlines A and B | chose B, shape 0.11 (fit 0.78, arc 36 deg, ratio 0.63, spill 0.02), edge ratio 1.16, box agreement 0.26, is_target=true, outline good, view side, edge visible 0.55, waterline visible; B follows the tyre outer edge above water with straight waterline, A extends into reflection below water | refine with box [617,658,706,709] px + points [626,694], [644,667], [682,668], [700,692] - points [664,683], [665,647], [665,738] | 7603 |
| segment | water | sam-2.1-hiera-small | box [512,663,1408,994] px + points [[896, 845]] | mask 219959 px (13% of photo) | verify | 686 |
| segment | bus wheel 1 | sam-2.1-hiera-small | box [617,658,706,709] px + points [626,694], [644,667], [682,668], [700,692] - points [664,683], [665,647], [665,738] | 6 proposals, 6 distinct; best shapes 0.50, 0.13 | verify the best two | 1883 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies entirely on the reflective flooded surface in the foreground surrounding the submerged truck. | accept | 6652 |
| verify | bus wheel 1 | muse-spark-open | crop [354,438,920,1004] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; Both magenta outlines trace the water reflection below the orange truck, not the black front tyre above the water whose upper arc is visible at the yellow box a | refine with box [637,605,698,653] px + points [641,642], [663,609], [691,618], [695,639] - points [669,630], [666,591], [668,681] | 9237 |
| segment | bus wheel 1 | sam-2.1-hiera-small | box [637,605,698,653] px + points [641,642], [663,609], [691,618], [695,639] - points [669,630], [666,591], [668,681] | 6 proposals, 6 distinct; best shapes 0.23, 0.02 | verify the best two | 1481 |
| verify | bus wheel 1 | muse-spark-open | crop [535,492,791,748] px with 2 outlines A and B | chose B, shape 0.02 (fit 0.58, arc 108 deg, ratio 0.41, spill 0.18), edge ratio 1.65, box agreement 0.47, is_target=true, outline good, view side, edge visible 0.75, waterline visible; B follows the black tyre outer edge above water closely without including the mudguard while A includes bodywork | give up: the outline covers a different area than the tyre (0.47) | 7584 |
| estimate | photo | muse-spark-open | no measurable wheel | 35 cm, sigma 20 cm | report model estimate | 0 |

### u_5T7dG1MRA3U: white crew cab truck (Downtown, Jacksonville, United States) (Unsplash close-up)

Author Wade Austin Ellis (@wadeaustinellis), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/5T7dG1MRA3U.

Result: **model_estimate**, depth 40.0 cm, sigma 20.0 cm, confidence 0.22.
Scene model's own estimate: 40.0 cm (sigma 20.0 cm), basis: Front tyre of pickup largely submerged with bow wave obscuring exact waterline indicating mid-tyre depth.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel size is known for a truck; no wheel or person found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Front tyre of pickup largely submerged with bow wave obscuring exact waterline indicating mid-tyre depth.

Wall time 14.3 s (timings ms: scene 14206); model cost USD 0.005496.

Overlay: `eval/out/u_5T7dG1MRA3U_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1143 px | water visible; references: none; own estimate 40 cm | no wheel: report own estimate | 14206 |
| estimate | photo | muse-spark-open | no measurable wheel | 40 cm, sigma 20 cm | report model estimate | 0 |

### u_ple6thPMX3c: A car is submerged in floodwater (Putney Embankment, London, UK) (Unsplash close-up)

Author John Cameron (@john_cameron), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/ple6thPMX3c.

Result: **model_estimate**, depth 70.0 cm, sigma 21.0 cm, confidence 0.23.
Scene model's own estimate: 70.0 cm (sigma 21.0 cm), basis: Floodwater submerges car to mid-doors and covers riverside walkway..
Captured at (EXIF): not recorded.

Reasons: standing water visible; car wheel 1: the scene model did not confirm the mask (A marks water/reflection under bumper not the wheel, B attempts the rear wheel but blob includes bumper/bodywork and does not follow tyre outer edge closely.); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Floodwater submerges car to mid-doors and covers riverside walkway..

Wall time 39.4 s (timings ms: scene 10740, agent 28578, geometry 0); model cost USD 0.034536.

Overlay: `eval/out/u_ple6thPMX3c_overlay.png`. Verifier images: `eval/out/u_ple6thPMX3c_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1536x2048 px | water visible; references: car_wheel; own estimate 70 cm | segment and verify 1 wheel and the water | 10740 |
| segment | water | sam-2.1-hiera-small | box [15,1477,1525,2041] px + points [[771, 1833]] | mask 837231 px (27% of photo) | verify | 1389 |
| segment | car wheel 1 | sam-2.1-hiera-small | box [633,1315,732,1394] px + points [647,1354], [677,1325], [710,1356] - points [676,1374], [676,1384], [637,1391] | 6 proposals, 6 distinct; best shapes 0.54, 0.41 | verify the best two | 2569 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only foreground, missing the flooded water immediately surrounding the submerged car. | refine with box [215,1147,1352,1516] px + point [785, 1315] | 8562 |
| segment | water | sam-2.1-hiera-small | box [215,1147,1352,1516] px + points [[785, 1315]] | mask 189967 px (6% of photo) | verify | 578 |
| verify | car wheel 1 | muse-spark-open | crop [515,1169,853,1507] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Both magenta outlines cover the rear bumper/bodywork corner, not the rear tyre which is submerged and not visible above the water. | refine with box [633,1315,732,1394] px - points [731,1319], [695,1333], [777,1352] | 9577 |
| segment | car wheel 1 | sam-2.1-hiera-small | box [633,1315,732,1394] px - points [731,1319], [695,1333], [777,1352] | 3 proposals, 3 distinct; best shapes 0.07, 0.00 | verify the best two | 223 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=false, coverage wrong; Magenta region is a submerged car, not the floodwater surface. | refine with box [323,1536,1260,2007] px + point [768, 1741] | 6059 |
| segment | water | sam-2.1-hiera-small | box [323,1536,1260,2007] px + points [[768, 1741]] | mask 286507 px (9% of photo) | verify | 460 |
| verify | car wheel 1 | muse-spark-open | crop [503,1189,841,1527] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Neither magenta outline follows the wheel tyre; A covers bumper/bodywork and B is a tiny sliver on the bumper, while the rear wheel is submerged under flood wat | refine with box [633,1315,732,1394] px - points [772,1368], [626,1365], [645,1426] | 6714 |
| segment | car wheel 1 | sam-2.1-hiera-small | box [633,1315,732,1394] px - points [772,1368], [626,1365], [645,1426] | 3 proposals, 3 distinct; best shapes 0.48, 0.11 | verify the best two | 227 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but only the foreground patch, missing the water immediately around the submerged car. | accept (coverage partial) | 8296 |
| verify | car wheel 1 | muse-spark-open | crop [363,1082,919,1638] px with 2 outlines A and B | chose B, shape 0.11 (fit 0.62, arc 150 deg, ratio 0.59, spill 0.13), edge ratio 0.98, not a tyre edge: 4 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.00, is_target=false, outline wrong, view side, edge visible 0.45, waterline visible; A marks water/reflection under bumper not the wheel, B attempts the rear wheel but blob includes bumper/bodywork and does not follow tyre outer edge closely. | give up | 9127 |
| estimate | photo | muse-spark-open | no measurable wheel | 70 cm, sigma 21 cm | report model estimate | 0 |

### u_Zo6hsIyy75o: a van is in a flooded area with trees in the background (Unsplash close-up)

Author Caspian Dahlström (@caspian_dahlstrom), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/Zo6hsIyy75o.

Result: **model_estimate**, depth 95.0 cm, sigma 28.5 cm, confidence 0.22.
Scene model's own estimate: 95.0 cm (sigma 28.5 cm), basis: Water reaches window sills of pickup and caravan indicating deep submersion.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel or person found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches window sills of pickup and caravan indicating deep submersion.

Wall time 7.2 s (timings ms: scene 7176); model cost USD 0.002972.

Overlay: `eval/out/u_Zo6hsIyy75o_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px | water visible; references: none; own estimate 95 cm | no wheel: report own estimate | 7176 |
| estimate | photo | muse-spark-open | no measurable wheel | 95 cm, sigma 28 cm | report model estimate | 0 |

### c1: Chennai flood water logging near ofice nandanam4.jpg (Commons, named in the brief)

Author Vijayingarsal, licence CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/), source https://commons.wikimedia.org/wiki/File:Chennai_flood_water_logging_near_ofice_nandanam4.jpg.

Result: **person_reference**, depth 45.6 cm, sigma 13.0 cm, confidence 0.22.
Scene model's own estimate: 36.0 cm (sigma 20.0 cm), basis: Water at knee level of adults and lower hub of scooters across flooded road.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (Both magenta outlines enclose license plate/mudguard and bodywork above water instead of following the round black tyre outer edge with rim inside.); two wheeler 2 wheel: the scene model did not confirm the mask (no wheel-like proposal in the vehicle box); person 1: the water just below was segmented locally, not checked by the scene model; person 2: the water just below was segmented locally, not checked by the scene model; a person standing in the water was measured against the adult stature prior (159 cm, sigma 9 cm); a person reference is coarse: expect about 10 to 15 cm of error; the scene model puts the water at the knee, about 45 cm (0 cm apart).

Wall time 48.5 s (timings ms: scene 8955, agent 35618, geometry 0); model cost USD 0.03233.

Overlay: `eval/out/c1_overlay.png`. Verifier images: `eval/out/c1_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1632x918 px | water visible; references: two_wheeler_wheel, person, person, two_wheeler_wheel; own estimate 36 cm | search 2 vehicle boxes for wheels, check 2 wheels and segment the water | 8955 |
| segment | water | sam-2.1-hiera-small | box [335,578,980,859] px + points [[607, 713]] | mask 139499 px (9% of photo) | verify | 1517 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [991,446,1063,478] px | 74 proposals from 24 grid points and 1 dark blobs; none wheel-like | give up | 3610 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [787,463,914,524] px | 75 proposals from 24 grid points and 1 dark blobs; best: wheel 0.29 = shape 0.35 (fit 0.97, arc 69 deg, ratio 0.67, spill 0.01) x dark 0.65 x size 1.00; wheel 0.16 = shape 0.28 (fit 1.00, arc 51 deg, ratio 0.39, spill 0.00) x dark 0.18 x size 1.00 | verify the best two | 5825 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only foreground, missing the flooded area around the wading people and motorbike. | refine with box [82,477,1077,597] px + point [653, 532] | 6465 |
| segment | water | sam-2.1-hiera-small | box [82,477,1077,597] px + points [[653, 532]] | mask 87835 px (6% of photo) | verify | 264 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta tint covers the brown floodwater surface surrounding the people and motorcycle within the yellow box. | accept | 4779 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [601,250,1086,735] px with 2 outlines A and B | chose A, shape 0.35 (fit 0.97, arc 69 deg, ratio 0.67, spill 0.01), edge ratio 0.82, not a tyre edge: 2 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.57, is_target=true, outline partial, view side, edge visible 0.55, waterline visible; B outline is in the water away from the wheel and completely wrong, A outline is on the rear wheel but is boxy and does not closely follow the round outer tyre  | refine with box [793,469,833,506] px + points [795,492], [804,476], [822,478], [830,492] - points [811,490], [844,476], [836,498] | 7408 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [793,469,833,506] px + points [795,492], [804,476], [822,478], [830,492] - points [811,490], [844,476], [836,498] | 6 proposals, 6 distinct; best shapes 0.45, 0.37 | verify the best two | 1519 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [690,346,946,602] px with 2 outlines A and B | chose A, shape 0.45 (fit 1.00, arc 81 deg, ratio 0.76, spill 0.00, closed ring), edge ratio 0.58, not a tyre edge: a darker ring lies just outside the outline (dark 0.55 outside, 0.22 inside): it follows the rim, inside the tyre, box agreement 0.24, is_target=false, outline wrong, view side, edge visible 0.35, waterline visible; A is tighter than B but its magenta still encloses red bodywork/mudguard above the black tyre and does not follow the tyre outer edge strictly. | refine with box [793,485,821,505] px + points [794,499], [802,487], [812,488], [819,499] - points [806,497], [815,487], [811,496] | 9689 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [793,485,821,505] px + points [794,499], [802,487], [812,488], [819,499] - points [806,497], [815,487], [811,496] | 6 proposals, 6 distinct; best shapes 0.09, 0.06 | verify the best two | 1397 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [682,364,938,620] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.20, waterline visible; Both magenta outlines enclose license plate/mudguard and bodywork above water instead of following the round black tyre outer edge with rim inside. | give up | 9652 |
| segment | people | sam-2.1-hiera-small | 2 person boxes with head and body points | 2 masks | measure each against the stature prior | 2169 |
| segment | water below people | sam-2.1-hiera-small | 2 bands just below people the verified water does not reach | 2 water masks | check that each outline ends at water | 1673 |
| geometry | person 1 | geometry | person and water masks | depth 45.6 cm, sigma 13.0 cm, 71% of stature above water; scene model says the water is at the knee (45 cm) | candidate | 6 |
| geometry | person 2 | geometry | person and water masks | depth 31.2 cm, sigma 15.4 cm, 80% of stature above water; scene model says the water is at the knee (45 cm) | candidate | 1 |
| estimate | photo | geometry | no measurable wheel; a measured person | depth 45.6 cm, sigma 13.0 cm | use the person | 0 |

### c2: India - Chennai - Monsoon - 04 (3058215637).jpg (Commons, named in the brief)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3058215637/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_04_(3058215637).jpg.

Result: **model_estimate**, depth 20.0 cm, sigma 20.0 cm, confidence 0.22.
Scene model's own estimate: 20.0 cm (sigma 20.0 cm), basis: Water covers lower quarter of motorcycle tyres and laps building plinths on flooded street.
Captured at (EXIF): 2008-10-22T10:04:35.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); two wheeler 2 wheel: the scene model did not confirm the mask (Neither outline follows the tyre: A is a water reflection below the wheel and B is the tarp edge above the wheel, the visible wheel is a wheelbarrow wheel not a); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers lower quarter of motorcycle tyres and laps building plinths on flooded street.

Wall time 31.3 s (timings ms: scene 9162, agent 21971, geometry 0); model cost USD 0.025532.

Overlay: `eval/out/c2_overlay.png`. Verifier images: `eval/out/c2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 20 cm | search 2 vehicle boxes for wheels, check 2 wheels and segment the water | 9162 |
| segment | water | sam-2.1-hiera-small | box [408,946,1783,1519] px + points [[1038, 1269]] | mask 731615 px (23% of photo) | verify | 1235 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [575,833,768,1057] px | 74 proposals from 24 grid points and 1 dark blobs; best: wheel 0.45 = shape 0.45 (fit 1.00, arc 81 deg, ratio 0.31, spill 0.00) x dark 0.98 x size 1.00; wheel 0.34 = shape 0.54 (fit 0.98, arc 102 deg, ratio 0.55, spill 0.00) x dark 0.70 x size 0.74 | verify the best two | 4536 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1037,754,1141,894] px | 78 proposals from 24 grid points and 2 dark blobs; best: wheel 0.45 = shape 0.54 (fit 0.89, arc 123 deg, ratio 0.74, spill 0.00) x dark 0.66 x size 1.00; wheel 0.26 = shape 0.27 (fit 1.00, arc 48 deg, ratio 0.70, spill 0.00, closed ring) x dark 0.92 x size 1.00 | verify the best two | 6115 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta overlay covers the flooded street water surface in foreground adjacent to sidewalk and road. | accept | 7079 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [516,789,784,1056] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.00, waterline not visible; Neither outline follows the tyre: A is a water reflection below the wheel and B is the tarp edge above the wheel, the visible wheel is a wheelbarrow wheel not a | give up | 8695 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [942,657,1198,913] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.35, waterline visible; Neither outline follows the narrow head-on tyre; A is splash at waterline and B is crash-bar/mudguard beside wheel. | refine with box [1077,777,1100,826] px + points [1079,813], [1086,793], [1096,814] - points [1087,807], [1087,767], [1070,826] | 7221 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1077,777,1100,826] px + points [1079,813], [1086,793], [1096,814] - points [1087,807], [1087,767], [1070,826] | 6 proposals, 6 distinct; best shapes 0.65, 0.45 | verify the best two | 1476 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [892,602,1285,994] px with 2 outlines A and B | chose A, shape 0.65 (fit 0.85, arc 165 deg, ratio 0.59, spill 0.00), edge ratio 0.76, not a tyre edge: a darker ring lies just outside the outline (dark 0.43 outside, 0.07 inside): it follows the rim, inside the tyre, box agreement 0.10, is_target=true, outline good, view head_on, edge visible 0.30, waterline visible; A follows the narrow upright front tyre seen head-on while B traces the splash water, and A's outline stays on the tyre rubber above the waterline. | give up: tyre seen head-on, its outline cannot be measured | 7023 |
| estimate | photo | muse-spark-open | no measurable wheel | 20 cm, sigma 20 cm | report model estimate | 0 |

### c3: Inundation of Chennai in 2017-3.jpg (Commons, named in the brief)

Author Kaitha Poo Manam, licence CC BY 3.0 (https://creativecommons.org/licenses/by/3.0/), source https://commons.wikimedia.org/wiki/File:Inundation_of_Chennai_in_2017-3.jpg.

Result: **person_reference**, depth 102.9 cm, sigma 10.9 cm, confidence 0.28.
Scene model's own estimate: 120.0 cm (sigma 36.0 cm), basis: Multiple adults show water at waist to chest height across street center..
Captured at (EXIF): 2015-12-02T11:04:31.

Reasons: standing water visible; person 1: the shoulder line could not be found; a person standing in the water was measured against the adult stature prior (159 cm, sigma 9 cm); a person reference is coarse: expect about 10 to 15 cm of error; the scene model puts the water at the waist, about 95 cm (8 cm apart).

Wall time 26.0 s (timings ms: scene 8407, agent 14185, geometry 0); model cost USD 0.008922.

Overlay: `eval/out/c3_overlay.png`. Verifier images: `eval/out/c3_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: person, person, person, person; own estimate 120 cm | no wheel: measure 4 persons against the stature prior and segment the water | 8407 |
| segment | water | sam-2.1-hiera-small | box [653,1404,1027,1499] px + points [[848, 1439]] | mask 21187 px (1% of photo) | verify | 1624 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but too small and misses the flooded street around wading people. | refine with box [164,1198,1434,1475] px + point [717, 1329] | 5967 |
| segment | water | sam-2.1-hiera-small | box [164,1198,1434,1475] px + points [[717, 1329]] | mask 120600 px (4% of photo) | verify | 1471 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta tint aligns with the murky floodwater covering the street around the wading people and vehicles. | accept | 4938 |
| segment | people | sam-2.1-hiera-small | 3 person boxes with head and body points | 3 masks | measure each against the stature prior | 3268 |
| geometry | person 1 | geometry | person mask | the shoulder line could not be found | declined | 1 |
| geometry | person 2 | geometry | person and water masks | depth 102.9 cm, sigma 10.9 cm, 35% of stature above water; scene model says the water is at the waist (95 cm) | candidate | 2 |
| geometry | person 3 | geometry | person and water masks | depth 97.0 cm, sigma 11.7 cm, 39% of stature above water; scene model says the water is at the hip (84 cm) | candidate | 1 |
| estimate | photo | geometry | no measurable wheel; a measured person | depth 102.9 cm, sigma 10.9 cm | use the person | 0 |

### c4: Chennai-floods-2015-dec-7.jpg (Commons, named in the brief)

Author Destination8infinity, licence CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/), source https://commons.wikimedia.org/wiki/File:Chennai-floods-2015-dec-7.jpg.

Result: **model_estimate**, depth 140.0 cm, sigma 42.0 cm, confidence 0.22.
Scene model's own estimate: 140.0 cm (sigma 42.0 cm), basis: Ground floor shops submerged to awning level and overturned auto almost covered.
Captured at (EXIF): 2015-12-02T07:56:12.

Reasons: standing water visible; no wheel or person found to measure against; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Ground floor shops submerged to awning level and overturned auto almost covered.

Wall time 9.1 s (timings ms: scene 9099); model cost USD 0.004227.

Overlay: `eval/out/c4_overlay.png`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 980x735 px | water visible; references: none; own estimate 140 cm | no wheel: report own estimate | 9099 |
| estimate | photo | muse-spark-open | no measurable wheel | 140 cm, sigma 42 cm | report model estimate | 0 |

### x1: India - Chennai - Monsoon - 07 (3059060932).jpg (Commons, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059060932/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_07_(3059060932).jpg.

Result: **model_estimate**, depth 18.0 cm, sigma 20.0 cm, confidence 0.22.
Scene model's own estimate: 18.0 cm (sigma 20.0 cm), basis: Water covers lower quarter of motorcycle tyres across flooded street.
Captured at (EXIF): 2008-10-22T10:06:36.

Reasons: standing water visible; the time budget of 55 s ran out before every check was done; this is the best answer reached in time; two wheeler 1 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); two wheeler 2 wheel: the scene model did not confirm the mask (time budget reached); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers lower quarter of motorcycle tyres across flooded street.

Wall time 50.2 s (timings ms: scene 10941, agent 39078, geometry 0); model cost USD 0.039794.

Overlay: `eval/out/x1_overlay.png`. Verifier images: `eval/out/x1_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel; own estimate 18 cm | search 4 vehicle boxes for wheels, check 3 wheels and segment the water | 10941 |
| segment | water | sam-2.1-hiera-small | box [27,958,1828,1525] px + points [[815, 1269]] | mask 825109 px (26% of photo) | verify | 1434 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [660,617,1641,1136] px | 84 proposals from 24 grid points and 4 dark blobs; best: wheel 0.45 = shape 0.79 (fit 0.97, arc 189 deg, ratio 0.37, spill 0.03, closed ring) x dark 0.14 x size 1.00; wheel 0.31 = shape 0.40 (fit 1.00, arc 78 deg, ratio 0.35, spill 0.01, closed ring) x dark 0.54 x size 1.00 | verify the best two | 8043 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [375,580,1109,1104] px | 84 proposals from 24 grid points and 4 dark blobs; best: wheel 0.37 = shape 0.47 (fit 0.87, arc 150 deg, ratio 0.62, spill 0.05, closed ring) x dark 0.61 x size 0.97; wheel 0.32 = shape 0.35 (fit 0.78, arc 132 deg, ratio 0.71, spill 0.04, closed ring) x dark 0.81 x size 1.00 | verify the best two | 8622 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region is open flooded street water in foreground connected to water around parked motorcycles. | accept | 8601 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [516,1,2048,1536] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.55, waterline visible; Neither outline follows tyre: A is water/debris, B is engine/bodywork, not the front tyre rim and rubber | refine with box [1411,854,1641,956] px + points [1417,738], [1455,577], [1507,744] - points [1463,735], [1455,503], [1474,904] | 7713 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [0,0,1642,1536] px with 2 outlines A and B | chose B, shape 0.35 (fit 0.78, arc 132 deg, ratio 0.71, spill 0.04, closed ring), edge ratio 0.66, box agreement 0.66, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B follows the black tyre outer edge closely above the straight waterline without including mudguard or exhaust, while A is looser. | refine with box [435,684,640,906] px + points [447,779], [524,700], [603,733], [624,819] - points [530,797], [534,690], [430,731] | 8876 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1411,854,1641,956] px + points [1417,738], [1455,577], [1507,744] - points [1463,735], [1455,503], [1474,904] | 6 proposals, 5 distinct; best shapes 0.33, 0.20 | verify the best two | 1863 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [435,684,640,906] px + points [447,779], [524,700], [603,733], [624,819] - points [530,797], [534,690], [430,731] | 6 proposals, 6 distinct; best shapes 0.32, 0.31 | verify the best two | 1333 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [1123,423,1893,1193] px with 2 outlines A and B | chose B, shape 0.20 (fit 0.70, arc 81 deg, ratio 0.48, spill 0.02, closed ring), edge ratio 1.25, not a tyre edge: the outline is an oval tilted 35 degrees: a cut or partial outline, not an upright wheel, box agreement 0.52, is_target=false, outline partial, view head_on, edge visible 0.65, waterline visible; A is only a small mark, B traces the front tyre but its magenta outline also encloses the chrome mudguard so it is not tight to the tyre outer edge. | refine with box [1408,656,1497,850] px + points [1423,791], [1434,700], [1471,702], [1482,790] - points [1449,751], [1429,740], [1445,823] | 7554 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1408,656,1497,850] px + points [1423,791], [1434,700], [1471,702], [1482,790] - points [1449,751], [1429,740], [1445,823] | 6 proposals, 6 distinct; best shapes 0.64, 0.42 | verify the best two | 1779 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [169,397,905,1132] px with 2 outlines A and B | chose A, shape 0.32 (fit 0.77, arc 96 deg, ratio 0.86, spill 0.00, closed ring), edge ratio 0.92, box agreement 0.62, is_target=false, outline wrong, view side, edge visible 0.65, waterline visible; Best outline still encloses the exhaust/muffler and spillover area, not just the tyre rubber and rim, so it will give wrong water depth. | refine with box [407,644,583,896] px + points [419,793], [472,660], [543,681], [566,798] - points [502,783], [550,776], [469,646] | 11335 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [407,644,583,896] px + points [419,793], [472,660], [543,681], [566,798] - points [502,783], [550,776], [469,646] | 6 proposals, 6 distinct; best shapes 0.74, 0.58 | verify the best two | 1738 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [1178,472,1724,1018] px with 2 outlines A and B | chose B, shape 0.42 (fit 0.95, arc 84 deg, ratio 0.32, spill 0.00), edge ratio 0.38, not a tyre edge: a darker ring lies just outside the outline (dark 0.70 outside, 0.04 inside): it follows the rim, inside the tyre, box agreement 0.01, is_target=true, outline good, view head_on, edge visible 0.60, waterline visible; B follows the narrow head-on tyre band above water while A includes mudguard and spokes outside tyre | give up: tyre seen head-on, its outline cannot be measured | 6535 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [174,401,890,1116] px | error: the time budget ran out during this check | give up | 0 |
| budget | photo | pipeline | time budget 55 s | 50 s used; checks still open were stopped | answer with what was verified in time | 0 |
| estimate | photo | muse-spark-open | no measurable wheel | 18 cm, sigma 20 cm | report model estimate | 0 |

### x2: India - Chennai - Monsoon - 02 (3059047958).jpg (Commons, side-on wheels)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3059047958/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_02_(3059047958).jpg.

Result: **model_estimate**, depth 19.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 19.0 cm (sigma 20.0 cm), basis: Water covers street and reaches lower third of motorcycle wheels on submerged road.
Captured at (EXIF): 2008-10-22T09:17:24.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers street and reaches lower third of motorcycle wheels on submerged road.

Wall time 22.0 s (timings ms: scene 8803, agent 13088, geometry 0); model cost USD 0.012487.

Overlay: `eval/out/x2_overlay.png`. Verifier images: `eval/out/x2_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 19 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 8803 |
| segment | water | sam-2.1-hiera-small | box [287,519,1230,1523] px + points [[588, 1005]] | mask 929146 px (30% of photo) | verify | 1418 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1091,856,1951,1382] px | 84 proposals from 24 grid points and 4 dark blobs; best: wheel 0.39 = shape 0.43 (fit 0.98, arc 81 deg, ratio 0.85, spill 0.00, closed ring) x dark 0.81 x size 0.99; wheel 0.37 = shape 0.59 (fit 0.96, arc 114 deg, ratio 0.50, spill 0.00, closed ring) x dark 0.27 x size 0.98 | verify the best two | 5243 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta tint overlays the central flooded street water surface around the motorcycle, covering contiguous open water. | accept | 5145 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [889,561,2048,1536] px with 2 outlines A and B | chose A, shape 0.43 (fit 0.98, arc 81 deg, ratio 0.85, spill 0.00, closed ring), edge ratio 1.74, box agreement 0.52, is_target=true, outline good, view head_on, edge visible 0.85, waterline visible; A tightly follows the narrow upright rear tyre band above water while B outlines water, so A is the tyre head-on with waterline at bottom. | give up: tyre seen head-on, its outline cannot be measured | 7699 |
| estimate | photo | muse-spark-open | no measurable wheel | 19 cm, sigma 20 cm | report model estimate | 0 |

<!-- real-eval:end -->

<!-- wheel-eval:begin -->
Generated by `uv run --extra local python ../eval/real_photos.py` on 2026-11-01 with muse-spark-open (meta-models/muse-spark-open-1026 on Baseten, scene reasoning effort low) and sam-2.1-hiera-small on mps (ready in 5.6 s), no time budget, each check cut after 120 s. No ground truth exists for these photos; only qualitative behaviour is shown.

### w_chennai01: India - Chennai - Monsoon - 01 (3058208937).jpg (Commons, chosen for the wheel method)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3058208937/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_01_(3058208937).jpg.

Result: **model_estimate**, depth 15.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 15.0 cm (sigma 20.0 cm), basis: Ankle-deep on road, deeper at truck tyres, shallower on footpath..
Captured at (EXIF): 2008-10-22T09:16:35.

Reasons: standing water visible; no wheel size is known for a truck; two wheeler 2 wheel: the scene model did not confirm the mask (A follows the tyre arc but wrongly includes the shock absorber and frame, B is a tiny non-tyre blob, so A is better but still not tyre-only.); person 1: the measured depth is outside the plausible range (-42 cm); two-wheeler wheel: too little of the wheel rim is visible above water (99 degrees visible); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Ankle-deep on road, deeper at truck tyres, shallower on footpath..

Wall time 135.5 s (timings ms: scene 2, agent 134526, geometry 298); model cost USD 0.028798.

Overlay: `eval/out/wheel-final/w_chennai01_overlay.png`. Verifier images: `eval/out/wheel-final/w_chennai01_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px (cached reading) | water visible; references: bus_wheel, bus_wheel, two_wheeler_wheel, two_wheeler_wheel, person; own estimate 15 cm | search 2 vehicle boxes for wheels, check 4 wheels and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [573,891,1394,1414] px + points [[922, 1152]] | mask 343075 px (11% of photo) | verify | 1807 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1294,549,1677,727] px | 90 proposals from 24 grid points, 2 scene model wheel boxes and 2 dark blobs; best: wheel 0.50 = shape 0.65 (fit 1.00, arc 117 deg, ratio 0.70, spill 0.00, closed ring) x dark 0.53 x size 1.00; wheel 0.44 = shape 0.53 (fit 0.92, arc 123 deg, ratio 0.90, spill 0.02, closed ring) x dark 0.69 x size 1.00 | verify the best two | 6414 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1306,1199,2048,1536] px | 81 proposals from 24 grid points and 3 dark blobs; best: wheel 0.21 = shape 0.29 (fit 0.81, arc 78 deg, ratio 0.44, spill 0.00, closed ring) x dark 0.45 x size 1.00; wheel 0.21 = shape 0.29 (fit 0.87, arc 69 deg, ratio 0.86, spill 0.00, closed ring) x dark 0.43 x size 1.00 | verify the best two | 10353 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [1382,531,1638,787] px with 1 outline | chose NONE, is_target=false, outline wrong, view side, edge visible 0.85, waterline visible; Magenta outline covers only the lower half of the tyre at the waterline and does not follow the round outer edge above water, missing most of the tyre. | refine with box [1468,566,1562,674] px + points [1474,628], [1511,573], [1548,621], [1556,654] - points [1514,627], [1491,617], [1514,655] | 20854 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1468,566,1562,674] px + points [1474,628], [1511,573], [1548,621], [1556,654] - points [1514,627], [1491,617], [1514,655] | 6 proposals, 5 distinct; best shapes 0.66, 0.56 | verify the best two | 1680 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region aligns with flooded street water around person's feet and vehicles. | accept | 32873 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [870,615,2048,1536] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Neither magenta outline follows the rear tyre; A encloses the rear mudguard and B encloses side bodywork, both miss the tyre's outer edge. | refine with box [1412,1269,1889,1536] px + points [1441,1407], [1518,1292], [1700,1287], [1848,1407] - points [1647,1449], [1465,1361], [1807,1398] | 87015 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [1412,1269,1889,1536] px + points [1441,1407], [1518,1292], [1700,1287], [1848,1407] - points [1647,1449], [1465,1361], [1807,1398] | 6 proposals, 6 distinct; best shapes 0.57, 0.42 | verify the best two | 2038 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [1343,450,1678,785] px with 2 outlines A and B | chose B, shape 0.56 (fit 0.91, arc 120 deg, ratio 0.81, spill 0.00, closed ring), edge ratio 1.12, box agreement 0.72, is_target=true, outline good, view side, edge visible 0.85, waterline visible; Outline B tightly follows the round outer tyre edge above the water with a straight waterline and excludes mudguard and suspension. | accept | 75785 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [805,612,2048,1536] px with 2 outlines A and B | chose B, shape 0.42 (fit 1.00, arc 75 deg, ratio 0.48, spill 0.00, closed ring), edge ratio 1.45, not a tyre edge: the outline is an oval tilted 27 degrees: a cut or partial outline, not an upright wheel, box agreement 0.52, is_target=true, outline good, view side, edge visible 0.60, waterline visible; B follows the black tyre outer edge closely above water while A includes the mudguard | refine with box [1476,1263,1843,1522] px + points [1501,1407], [1601,1296], [1750,1291], [1818,1397] - points [1656,1430], [1482,1347], [1793,1430] | 13451 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [1476,1263,1843,1522] px + points [1501,1407], [1601,1296], [1750,1291], [1818,1397] - points [1656,1430], [1482,1347], [1793,1430] | 6 proposals, 5 distinct; best shapes 0.55, 0.40 | verify the best two | 3434 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1090,835,2048,1536] px with 2 outlines A and B | chose A, shape 0.55 (fit 0.98, arc 105 deg, ratio 0.38, spill 0.00, closed ring), edge ratio 0.70, not a tyre edge: 2 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.79, is_target=false, outline partial, view side, edge visible 0.60, waterline visible; A follows the tyre arc but wrongly includes the shock absorber and frame, B is a tiny non-tyre blob, so A is better but still not tyre-only. | give up | 16312 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 8091 px of water added next to the tyres | join to the verified water | 1564 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points (segmented during the checks) | 1 masks | measure each against the stature prior | 955 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 6352 |
| geometry | person 1 | geometry | person mask | the measured depth is outside the plausible range (-42 cm) | declined | 4 |
| geometry | two wheeler wheel 1 | geometry | wheel and water masks | too little of the wheel rim is visible above water (99 degrees visible) | rejected | 292 |
| estimate | photo | muse-spark-open | no measurable wheel | 15 cm, sigma 20 cm | report model estimate | 0 |

### w_chennai03: India - Chennai - Monsoon - 03 (3058213357).jpg (Commons, chosen for the wheel method)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3058213357/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_03_(3058213357).jpg.

Result: **model_estimate**, depth 27.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 27.0 cm (sigma 20.0 cm), basis: water submerges lower third of parked motorcycle tyres and reaches auto wheel hubs on flooded street.
Captured at (EXIF): 2008-10-22T09:17:44.

Reasons: standing water visible; auto rickshaw 1 wheel: the scene model did not confirm the mask (Outline A best follows a tyre but it is the motorcycle rear tyre on the right, not the auto-rickshaw wheel on the left which is not visible, so it is the wrong ); two wheeler 2 wheel: the scene model did not confirm the mask (B is only mudguard, A follows tyre outer edge but incorrectly includes fork/mudguard inside outline so not tyre-only); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water submerges lower third of parked motorcycle tyres and reaches auto wheel hubs on flooded street.

Wall time 86.6 s (timings ms: scene 4, agent 86391, geometry 0); model cost USD 0.049653.

Overlay: `eval/out/wheel-final/w_chennai03_overlay.png`. Verifier images: `eval/out/wheel-final/w_chennai03_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel; own estimate 27 cm | search 3 vehicle boxes for wheels, check 3 wheels and segment the water | 4 |
| segment | water | sam-2.1-hiera-small | box [1065,799,2045,1534] px + points [[1536, 1152]] | mask 534805 px (17% of photo) | verify | 1191 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [944,277,1650,710] px | 102 proposals from 24 grid points, 3 scene model wheel boxes and 4 dark blobs; best: wheel 0.62 = shape 0.78 (fit 0.95, arc 156 deg, ratio 0.82, spill 0.00, closed ring) x dark 0.58 x size 1.00; wheel 0.57 = shape 0.65 (fit 0.99, arc 120 deg, ratio 0.88, spill 0.00, closed ring) x dark 0.75 x size 1.00 | verify the best two | 12768 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but yellow box crops to lower right and misses water directly around and behind the auto-rickshaw. | refine with box [92,891,1198,1513] px + point [635, 1260] | 13264 |
| segment | water | sam-2.1-hiera-small | box [92,891,1198,1513] px + points [[635, 1260]] | mask 452999 px (14% of photo) | verify | 572 |
| segment | auto rickshaw 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [0,469,1151,1219] px | 84 proposals from 24 grid points and 4 dark blobs; best: wheel 0.32 = shape 0.49 (fit 0.89, arc 114 deg, ratio 0.56, spill 0.00, closed ring) x dark 0.78 x size 0.73; wheel 0.26 = shape 0.29 (fit 0.94, arc 105 deg, ratio 0.67, spill 0.09, closed ring) x dark 0.78 x size 1.00 | verify the best two | 22194 |
| verify | auto rickshaw 1 wheel | muse-spark-open | crop [784,339,1206,761] px with 1 outline | chose A, shape 0.49 (fit 0.89, arc 114 deg, ratio 0.56, spill 0.00, closed ring), edge ratio 1.55, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Magenta outline traces the motorcycle rear tyre on the right, not the auto-rickshaw wheel which is not visible in this crop. | refine with box [948,469,1042,631] px - points [995,550], [847,634], [1100,487] | 15633 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is turbulent wake water but misses most flooded street around vehicles. | refine with box [1065,430,2017,1352] px + point [1536, 799] | 25246 |
| segment | auto rickshaw 1 wheel | sam-2.1-hiera-small | box [948,469,1042,631] px - points [995,550], [847,634], [1100,487] | 3 proposals, 3 distinct; best shapes 0.48, 0.38 | verify the best two | 2640 |
| segment | water | sam-2.1-hiera-small | box [1065,430,2017,1352] px + points [[1536, 799]] | mask 545124 px (17% of photo) | verify | 263 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1146,79,1822,755] px with 2 outlines A and B | chose A, shape 0.78 (fit 0.95, arc 156 deg, ratio 0.82, spill 0.00, closed ring), edge ratio 0.89, box agreement 0.59, is_target=true, outline good, view side, edge visible 0.65, waterline visible; A follows the front tyre outer edge above water with rim inside and straight waterline, B is on engine area not the tyre. | refine with box [1389,346,1582,502] px + points [1406,458], [1467,360], [1541,360], [1568,431] - points [1487,447], [1447,380], [1474,420] | 28702 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [1389,346,1582,502] px + points [1406,458], [1467,360], [1541,360], [1568,431] - points [1487,447], [1447,380], [1474,420] | 6 proposals, 5 distinct; best shapes 0.74, 0.34 | verify the best two | 1999 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but only covers right side and misses water around the auto-rickshaw. | accept (coverage partial) | 20514 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1208,124,1796,712] px with 2 outlines A and B | chose A, shape 0.74 (fit 0.99, arc 138 deg, ratio 0.83, spill 0.01, closed ring), edge ratio 0.89, not a tyre edge: 2 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.49, is_target=true, outline good, view side, edge visible 0.75, waterline visible; Outline A follows the tyre outer edge above water with rim inside and a straight waterline, while B only marks a small rim fragment. | refine with box [1390,330,1590,521] px + points [1397,453], [1481,336], [1573,453], [1528,356] - points [1493,459], [1487,321], [1434,421] | 18069 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [1390,330,1590,521] px + points [1397,453], [1481,336], [1573,453], [1528,356] - points [1493,459], [1487,321], [1434,421] | 6 proposals, 5 distinct; best shapes 0.55, 0.46 | verify the best two | 2375 |
| verify | auto rickshaw 1 wheel | muse-spark-open | crop [762,308,1230,776] px with 2 outlines A and B | chose A, shape 0.48 (fit 1.00, arc 87 deg, ratio 0.77, spill 0.00, closed ring), edge ratio 1.22, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Best outline follows the motorcycle rear tyre, not the auto-rickshaw wheel which is not visible in the crop, so it is the wrong object for measuring the auto-ri | refine with box [948,469,1042,631] px - points [1005,542], [1066,495], [902,636] | 23736 |
| segment | auto rickshaw 1 wheel | sam-2.1-hiera-small | box [948,469,1042,631] px - points [1005,542], [1066,495], [902,636] | 3 proposals, 3 distinct; best shapes 0.50, 0.10 | verify the best two | 544 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1211,127,1793,710] px with 2 outlines A and B | chose A, shape 0.55 (fit 1.00, arc 99 deg, ratio 0.87, spill 0.00, closed ring), edge ratio 0.71, not a tyre edge: the outline is an oval tilted 41 degrees: a cut or partial outline, not an upright wheel, box agreement 0.47, is_target=false, outline partial, view side, edge visible 0.65, waterline visible; B is only mudguard, A follows tyre outer edge but incorrectly includes fork/mudguard inside outline so not tyre-only | give up | 18800 |
| verify | auto rickshaw 1 wheel | muse-spark-open | crop [753,289,1247,783] px with 2 outlines A and B | chose A, shape 0.50 (fit 1.00, arc 90 deg, ratio 0.74, spill 0.00, closed ring), edge ratio 1.22, is_target=false, outline wrong, view side, edge visible 0.65, waterline visible; Outline A best follows a tyre but it is the motorcycle rear tyre on the right, not the auto-rickshaw wheel on the left which is not visible, so it is the wrong  | give up | 21342 |
| estimate | photo | muse-spark-open | no measurable wheel | 27 cm, sigma 20 cm | report model estimate | 0 |

### w_chennai12: India - Chennai - Monsoon - 12 (3100751476).jpg (Commons, chosen for the wheel method)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/3100751476/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Chennai_-_Monsoon_-_12_(3100751476).jpg.

Result: **model_estimate**, depth 12.0 cm, sigma 20.0 cm, confidence 0.2.
Scene model's own estimate: 12.0 cm (sigma 20.0 cm), basis: ankle-deep water across street with splash from moving bike.
Captured at (EXIF): 2008-11-27T16:49:20.

Reasons: standing water visible; no wheel size is known for a truck; auto rickshaw 1 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); auto-rickshaw wheel 2: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); person 1: the person is too small in the photo to measure (57 px tall); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: ankle-deep water across street with splash from moving bike.

Wall time 35.3 s (timings ms: scene 2, agent 35134, geometry 0); model cost USD 0.035216.

Overlay: `eval/out/wheel-final/w_chennai12_overlay.png`. Verifier images: `eval/out/wheel-final/w_chennai12_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px (cached reading) | water visible; references: auto_wheel, person, auto_wheel; own estimate 12 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [369,492,861,800] px + points [[614, 630]] | mask 175576 px (6% of photo) | verify | 2239 |
| segment | auto wheel 2 | sam-2.1-hiera-small | box [1710,607,1813,723] px + points [1724,660], [1761,622], [1798,664] - points [1761,645], [1761,607] | 6 proposals, 4 distinct; best shapes 0.61, 0.22 | verify the best two | 3915 |
| segment | auto rickshaw 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [0,231,560,520] px | 90 proposals from 24 grid points, 1 scene model wheel box and 4 dark blobs; best: wheel 0.46 = shape 0.46 (fit 0.96, arc 90 deg, ratio 0.51, spill 0.00, closed ring) x dark 0.99 x size 1.00; wheel 0.25 = shape 0.46 (fit 0.95, arc 93 deg, ratio 0.63, spill 0.00, closed ring) x dark 0.08 x size 1.00 | verify the best two | 7537 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but only a small patch near the auto and misses the flooded area around the person and vehicles. | refine with box [655,799,1475,1352] px + point [1024, 1075] | 8755 |
| segment | water | sam-2.1-hiera-small | box [655,799,1475,1352] px + points [[1024, 1075]] | mask 362806 px (12% of photo) | verify | 1201 |
| verify | auto wheel 2 | muse-spark-open | crop [1525,443,1986,904] px with 2 outlines A and B | chose A, shape 0.61 (fit 0.98, arc 114 deg, ratio 0.52, spill 0.00, closed ring), edge ratio 0.94, not a tyre edge: the outline is an oval tilted 33 degrees: a cut or partial outline, not an upright wheel, box agreement 0.69, is_target=true, outline partial, view side, edge visible 0.55, waterline visible; A follows the small dark tyre under the green body above the water while B is a huge splash/water region far outside the tyre. | refine with box [1724,608,1816,682] px + points [1730,639], [1762,616], [1799,630], [1806,660] - points [1769,644], [1758,588], [1756,720] | 10078 |
| segment | auto wheel 2 | sam-2.1-hiera-small | box [1724,608,1816,682] px + points [1730,639], [1762,616], [1799,630], [1806,660] - points [1769,644], [1758,588], [1756,720] | 6 proposals, 3 distinct; best shapes 0.57, 0.57 | verify the best two | 1014 |
| verify | auto rickshaw 1 wheel | muse-spark-open | crop [72,40,664,632] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.35, waterline visible; Neither outline follows the front tyre; A outlines the seat and B outlines the front bodywork/mudguard, both far from the partially submerged front wheel. | refine with box [467,357,529,401] px + points [476,386], [495,363], [516,386] - points [495,380], [495,339], [442,289] | 7461 |
| segment | auto rickshaw 1 wheel | sam-2.1-hiera-small | box [467,357,529,401] px + points [476,386], [495,363], [516,386] - points [495,380], [495,339], [442,289] | 6 proposals, 6 distinct; best shapes 0.42, 0.31 | verify the best two | 1073 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the flooded street water around the pedestrian and vehicles. | accept | 6097 |
| verify | auto wheel 2 | muse-spark-open | crop [1634,509,1910,784] px with 2 outlines A and B | chose B, shape 0.57 (fit 1.00, arc 102 deg, ratio 0.68, spill 0.00, closed ring), edge ratio 0.97, box agreement 0.52, is_target=true, outline good, view head_on, edge visible 0.55, waterline visible; B follows the narrow upright tyre seen edge-on while A is a large blob covering water and ground not the tyre | give up: tyre seen head-on, its outline cannot be measured | 9294 |
| verify | auto rickshaw 1 wheel | muse-spark-open | crop [362,248,618,504] px with 2 outlines A and B | chose B, shape 0.31 (fit 0.60, arc 174 deg, ratio 0.56, spill 0.02), edge ratio 0.65, not a tyre edge: a darker ring lies just outside the outline (dark 0.27 outside, 0.00 inside): it follows the rim, inside the tyre, box agreement 0.01, is_target=false, outline wrong, view head_on, edge visible 0.35, waterline visible; Both outlines trace the yellow mudguard/fender above the wheel, not the black tyre rubber below it, so neither follows the tyre outer edge and would give wrong  | refine with box [454,404,532,440] px + points [461,427], [471,414], [512,416], [523,428] - points [491,423], [491,388], [413,338] | 9304 |
| segment | auto rickshaw 1 wheel | sam-2.1-hiera-small | box [454,404,532,440] px + points [461,427], [471,414], [512,416], [523,428] - points [491,423], [491,388], [413,338] | 6 proposals, 6 distinct; best shapes 0.52, 0.41 | verify the best two | 1604 |
| verify | auto rickshaw 1 wheel | muse-spark-open | crop [360,292,616,548] px with 2 outlines A and B | chose A, shape 0.52 (fit 0.95, arc 105 deg, ratio 0.44, spill 0.00), edge ratio 1.79, box agreement 0.81, is_target=true, outline good, view head_on, edge visible 0.35, waterline visible; A follows the black tyre rubber tightly above the waterline without including mudguard, B adds extra lobe on left side | give up: tyre seen head-on, its outline cannot be measured | 7912 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points (segmented during the checks) | 1 masks | measure each against the stature prior | 1408 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 4194 |
| geometry | person 1 | geometry | person mask | the person is too small in the photo to measure (57 px tall) | declined | 0 |
| estimate | photo | muse-spark-open | no measurable wheel | 12 cm, sigma 20 cm | report model estimate | 0 |

### w_kanchipuram: India - Kanchipuram - 010 (1885517933).jpg (Commons, chosen for the wheel method)

Author McKay Savage (Flickr, https://www.flickr.com/photos/mckaysavage/1885517933/), licence CC BY 2.0 (https://creativecommons.org/licenses/by/2.0/), source https://commons.wikimedia.org/wiki/File:India_-_Kanchipuram_-_010_(1885517933).jpg.

Result: **wheel_geometry**, depth 4.5 cm, sigma 5.6 cm, confidence 0.16.
Scene model's own estimate: 12.0 cm (sigma 20.0 cm), basis: Ankle-deep water across road with tyre bottoms submerged.
Captured at (EXIF): 2007-11-04T15:20:58.

Reasons: standing water visible; two-wheeler wheel found; wheel size unknown, generic prior used; scene model waterline agrees within 3.7 cm; a person in the water gives 27 cm (sigma 12 cm), consistent with the wheel.

Wall time 49.2 s (timings ms: scene 1, agent 48499, geometry 495); model cost USD 0.021687.

Overlay: `eval/out/wheel-final/w_kanchipuram_overlay.png`. Verifier images: `eval/out/wheel-final/w_kanchipuram_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, person, person; own estimate 12 cm | search 3 vehicle boxes for wheels, check 2 wheels and segment the water | 1 |
| segment | water | sam-2.1-hiera-small | box [778,1044,1394,1491] px + points [[1049, 1247]] | mask 140290 px (4% of photo) | verify | 2942 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [657,734,995,881] px | 89 proposals from 24 grid points, 1 scene model wheel box and 4 dark blobs; best: wheel 0.67 = shape 0.77 (fit 1.00, arc 138 deg, ratio 0.90, spill 0.00, closed ring) x dark 0.74 x size 1.00; wheel 0.62 = shape 0.73 (fit 1.00, arc 132 deg, ratio 0.86, spill 0.00, closed ring) x dark 0.69 x size 1.00 | verify the best two | 7016 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1558,877,1970,1173] px | 90 proposals from 24 grid points, 1 scene model wheel box and 4 dark blobs; best: wheel 0.38 = shape 0.45 (fit 0.97, arc 123 deg, ratio 0.41, spill 0.06, closed ring) x dark 0.67 x size 1.00; wheel 0.33 = shape 0.51 (fit 0.97, arc 99 deg, ratio 0.86, spill 0.00, closed ring) x dark 0.30 x size 1.00 | verify the best two | 10795 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [618,672,874,928] px with 2 outlines A and B | chose A, shape 0.77 (fit 1.00, arc 138 deg, ratio 0.90, spill 0.00, closed ring), edge ratio 1.07, box agreement 0.88, is_target=true, outline good, view side, edge visible 0.75, waterline visible; A follows the outer black tyre edge above the water while B cuts inside the tyre on the upper right. | accept | 12077 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta tint lies on the brown floodwater covering the street around the standing people and motorcycles. | accept | 16435 |
| validate | two wheeler 2 wheel | validator | answer to verify two wheeler 2 wheel | tyre_box: [[305, 185], [590, 695]] is not a box [x_min, y_min, x_max, y_max]; dropped; corrected_box: [[305, 185], [590, 695]] is not a box [x_min, y_min, x_max, y_max]; dropped | drop 2 fields that failed, use the rest | 0 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1689,819,2040,1170] px with 2 outlines A and B | chose A, shape 0.45 (fit 0.97, arc 123 deg, ratio 0.41, spill 0.06, closed ring), edge ratio 0.86, not a tyre edge: 3 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, is_target=false, outline wrong, view oblique, edge visible 0.60, waterline visible; A is closer to front wheel but its magenta polygon cuts inside the rim and misses the outer tyre edge, so it does not follow the tyre for water-depth measuremen | refine with box [1797,942,1853,1054] px + points [1803,1002], [1821,928], [1866,937], [1880,994] - points [1831,989], [1879,966], [1829,1086] | 11477 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [1797,942,1853,1054] px + points [1803,1002], [1821,928], [1866,937], [1880,994] - points [1831,989], [1879,966], [1829,1086] | 6 proposals, 6 distinct; best shapes 0.47, 0.16 | verify the best two | 1445 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1660,826,2006,1171] px with 2 outlines A and B | chose B, shape 0.16 (fit 0.74, arc 54 deg, ratio 0.63, spill 0.00, closed ring), edge ratio 1.21, not a tyre edge: 3 of 3 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.68, is_target=true, outline good, view head_on, edge visible 0.65, waterline visible; B follows the narrow upright tyre band closely while A strays onto the mudguard and fork, so B gives the correct water depth. | give up: tyre seen head-on, its outline cannot be measured | 23329 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 3723 px of water added next to the tyres | join to the verified water | 1227 |
| segment | people | sam-2.1-hiera-small | 2 person boxes with head and body points (segmented during the checks) | 2 masks | measure each against the stature prior | 2070 |
| segment | water below people | sam-2.1-hiera-small | 2 bands just below people (segmented during the checks) | 2 water masks | check that each outline ends at water | 7352 |
| geometry | person 1 | geometry | person mask | the person is not standing upright (bending) | declined | 0 |
| geometry | person 2 | geometry | person and water masks | depth 27.2 cm, sigma 12.0 cm, 83% of stature above water; scene model says the water is at the ankle (6 cm) | candidate | 3 |
| geometry | two wheeler wheel 1 | geometry | wheel and water masks | depth 4.5 cm, sigma 4.3 cm, 195 degrees of rim visible | candidate | 494 |
| estimate | photo | geometry | measurement 4.5 cm and model estimate 12 cm | difference 8 cm, two combined sigma 42 cm | use the measurement | 0 |
| estimate | photo | geometry | wheel 4.5 cm and person 27.2 cm | difference 23 cm, two combined sigma 27 cm | use the wheel | 0 |

### w_quangngai: Inundo-Quang Ngai-3.jpg (Commons, chosen for the wheel method)

Author RG72, licence CC BY-SA 3.0 (https://creativecommons.org/licenses/by-sa/3.0/), source https://commons.wikimedia.org/wiki/File:Inundo-Quang_Ngai-3.jpg.

Result: **model_estimate**, depth 30.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 30.0 cm (sigma 20.0 cm), basis: Water covers road and reaches shin on motorbike and ankle on pedestrian.
Captured at (EXIF): 2013-11-16T14:02:03.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); two wheeler 2 wheel: the scene model did not confirm the mask (Neither magenta outline follows the bicycle tyre; A encloses the person's leg and frame and B is a tiny sliver on the seat stay, both missing the round tyre edg); person 1: the person is not standing upright (sitting); person 2: measured 111 cm (sigma 11 cm), but the scene model sees about 6 cm there; not used; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers road and reaches shin on motorbike and ankle on pedestrian.

Wall time 90.1 s (timings ms: scene 3, agent 89960, geometry 0); model cost USD 0.046119.

Overlay: `eval/out/wheel-final/w_quangngai_overlay.png`. Verifier images: `eval/out/wheel-final/w_quangngai_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 2048x1536 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, person, person; own estimate 30 cm | search 2 vehicle boxes for wheels, check 3 wheels and segment the water | 3 |
| segment | water | sam-2.1-hiera-small | box [1106,1044,1680,1506] px + points [[1331, 1275]] | mask 345734 px (11% of photo) | verify | 2546 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [1611,706,1988,923] px | 88 proposals from 24 grid points, 2 scene model wheel boxes and 2 dark blobs; best: wheel 0.59 = shape 0.68 (fit 1.00, arc 123 deg, ratio 0.43, spill 0.00, closed ring) x dark 0.74 x size 1.00; wheel 0.24 = shape 0.39 (fit 0.94, arc 81 deg, ratio 0.70, spill 0.00, closed ring) x dark 0.69 x size 0.71 | verify the best two | 6889 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [581,898,1091,1399] px | 90 proposals from 24 grid points, 1 scene model wheel box and 4 dark blobs; best: wheel 0.47 = shape 0.47 (fit 1.00, arc 84 deg, ratio 0.36, spill 0.00, closed ring) x dark 1.00 x size 1.00; wheel 0.29 = shape 0.29 (fit 0.77, arc 105 deg, ratio 0.62, spill 0.03) x dark 1.00 x size 1.00 | verify the best two | 10326 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but only a small foreground square, missing most flooded street around people and vehicles. | refine with box [573,645,1884,1198] px + point [1126, 845] | 22868 |
| validate | two wheeler 1 wheel | validator | answer to verify two wheeler 1 wheel | tyre_box: [[585, 470], [980, 830]] is not a box [x_min, y_min, x_max, y_max]; dropped; corrected_box: [[585, 470], [980, 830]] is not a box [x_min, y_min, x_max, y_max]; dropped | drop 2 fields that failed, use the rest | 0 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [417,754,1070,1407] px with 2 outlines A and B | chose B, shape 0.29 (fit 0.77, arc 105 deg, ratio 0.62, spill 0.03), edge ratio 1.38, not a tyre edge: 3 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Neither outline follows the front tyre's outer edge; B is near the wheel but is a small sliver behind the fork, not the tyre, while A is on the bodywork. | refine with box [647,955,706,1162] px + points [815,1198], [894,1084], [995,1107], [1034,1198] - points [923,1234], [871,1045], [903,1182] | 16157 |
| segment | water | sam-2.1-hiera-small | box [573,645,1884,1198] px + points [[1126, 845]] | mask 829822 px (26% of photo) | verify | 1178 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [647,955,706,1162] px + points [815,1198], [894,1084], [995,1107], [1034,1198] - points [923,1234], [871,1045], [903,1182] | 6 proposals, 5 distinct; best shapes 0.42, 0.34 | verify the best two | 1169 |
| validate | two wheeler 2 wheel | validator | answer to verify two wheeler 2 wheel | tyre_box: [[595, 285], [1000, 625]] is not a box [x_min, y_min, x_max, y_max]; dropped; corrected_box: [[595, 285], [1000, 625]] is not a box [x_min, y_min, x_max, y_max]; dropped | drop 2 fields that failed, use the rest | 0 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1457,541,1961,1046] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; Neither outline follows the rear bicycle tyre outer edge above water; A is a small fragment inside the wheel and B outlines the person's legs. | refine with box [1766,759,1806,837] px + points [1777,786], [1853,700], [1933,710] - points [1868,796], [1893,715], [1712,786] | 22829 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [1766,759,1806,837] px + points [1777,786], [1853,700], [1933,710] - points [1868,796], [1893,715], [1712,786] | 6 proposals, 4 distinct; best shapes 0.38, 0.00 | verify the best two | 1303 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [434,734,1126,1425] px with 2 outlines A and B | chose B, shape 0.34 (fit 0.74, arc 129 deg, ratio 0.61, spill 0.03), edge ratio 1.25, not a tyre edge: 3 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.19, is_target=false, outline wrong, view side, edge visible 0.45, waterline visible; B is closer than A but its magenta loop includes the red mudguard and silver fork and does not follow the black tyre outer edge. | refine with box [828,1093,1033,1266] px + points [839,1183], [877,1111], [977,1111], [1019,1183] - points [939,1204], [905,1080], [884,1183] | 21262 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [828,1093,1033,1266] px + points [839,1183], [877,1111], [977,1111], [1019,1183] - points [939,1204], [905,1080], [884,1183] | 6 proposals, 4 distinct; best shapes 0.33, 0.20 | verify the best two | 1471 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta overlay aligns with brown floodwater surrounding motorbike and bicycles. | accept | 35331 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1658,668,1914,924] px with 2 outlines A and B | chose A, shape 0.38 (fit 1.00, arc 69 deg, ratio 0.30, spill 0.00, closed ring), edge ratio 3.65, not a tyre edge: 3 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.00, is_target=true, outline good, view side, edge visible 0.65, waterline visible; A follows the outer black tyre edge above water with rim inside while B is only a small mudguard fragment | refine with box [1658,717,1766,870] px + points [1771,816], [1791,740], [1858,722], [1906,765] - points [1876,834], [1817,735], [1824,776] | 34826 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [1658,717,1766,870] px + points [1771,816], [1791,740], [1858,722], [1906,765] - points [1876,834], [1817,735], [1824,776] | 6 proposals, 6 distinct; best shapes 0.53, 0.35 | verify the best two | 1510 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [617,866,1254,1504] px with 2 outlines A and B | chose A, shape 0.33 (fit 0.93, arc 69 deg, ratio 0.44, spill 0.00, closed ring), edge ratio 2.67, not a tyre edge: the outline is an oval tilted 27 degrees: a cut or partial outline, not an upright wheel, box agreement 0.56, is_target=true, outline partial, view head_on, edge visible 0.55, waterline visible; A follows the black tyre edge head-on without including fork/mudguard while B encloses fork and bodywork, but A only traces the right side so it is partial. | give up: tyre seen head-on, its outline cannot be measured | 24905 |
| validate | two wheeler 2 wheel | validator | answer to verify two wheeler 2 wheel | tyre_box: [[575, 285], [1000, 685]] is not a box [x_min, y_min, x_max, y_max]; dropped; corrected_box: [[575, 285], [1000, 685]] is not a box [x_min, y_min, x_max, y_max]; dropped | drop 2 fields that failed, use the rest | 0 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [1507,569,1959,1021] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; Neither magenta outline follows the bicycle tyre; A encloses the person's leg and frame and B is a tiny sliver on the seat stay, both missing the round tyre edg | give up | 22351 |
| segment | people | sam-2.1-hiera-small | 2 person boxes with head and body points (segmented during the checks) | 2 masks | measure each against the stature prior | 1728 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 7023 |
| geometry | person 1 | geometry | person mask | the person is not standing upright (sitting) | declined | 0 |
| geometry | person 2 | geometry | person and water masks | depth 110.6 cm, sigma 10.5 cm, 30% of stature above water; scene model says the water is at the ankle (6 cm) | candidate | 5 |
| estimate | people | geometry | 1 people measured | every one disagrees with the scene model beyond two sigma | do not publish a person measurement | 0 |
| estimate | photo | muse-spark-open | no measurable wheel | 30 cm, sigma 20 cm | report model estimate | 0 |

### w_kerala4x4: Kerala police 4x4 driving through a monsoon.jpg (Commons, chosen for the wheel method)

Author Liji Jinaraj (Flickr), from Kochi, India, licence CC BY-SA 2.0 (https://creativecommons.org/licenses/by-sa/2.0/), source https://commons.wikimedia.org/wiki/File:Kerala_police_4x4_driving_through_a_monsoon.jpg.

Result: **model_estimate**, depth 35.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 35.0 cm (sigma 20.0 cm), basis: Water submerges lower third to half of vehicle tyres across the flooded street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel size is known for a other; two-wheeler wheel found; wheel size unknown, generic prior used; scene model waterline agrees within 0.8 cm; car wheel 2: the scene model did not confirm the mask (outline and the tyre's box disagree); car wheel 3: the scene model did not confirm the mask (outline and the tyre's box disagree); the two-wheeler wheel measured 15.3 cm (sigma 5.0 cm), but the scene model estimates 32 cm at that wheel, above the measurement's two-sigma bound; a shallow reading that disagrees is not trusted, so the scene model's estimate is reported; estimate based on: Water submerges lower third to half of vehicle tyres across the flooded street.

Wall time 157.0 s (timings ms: scene 4, agent 156142, geometry 640); model cost USD 0.05019.

Overlay: `eval/out/wheel-final/w_kerala4x4_overlay.png`. Verifier images: `eval/out/wheel-final/w_kerala4x4_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1200 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, car_wheel, car_wheel; own estimate 35 cm | search 1 vehicle box for wheels, check 4 wheels and segment the water | 4 |
| segment | water | sam-2.1-hiera-small | box [227,722,1460,1197] px + points [[779, 1016]] | mask 494782 px (26% of photo) | verify | 1217 |
| segment | car wheel 2 | sam-2.1-hiera-small | box [1021,606,1164,723] px + points [1040,630], [1091,606], [1144,636] - points [1091,642], [1091,618], [1168,660] | 6 proposals, 5 distinct; best shapes 0.63, 0.25 | verify the best two | 2887 |
| segment | car wheel 3 | sam-2.1-hiera-small | box [1216,608,1329,709] px + points [1232,630], [1272,612], [1315,636] - points [1272,636], [1272,618], [1336,660] | 6 proposals, 6 distinct; best shapes 0.49, 0.40 | verify the best two | 3763 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [148,567,581,757] px | 96 proposals from 24 grid points, 2 scene model wheel boxes and 4 dark blobs; best: wheel 0.56 = shape 0.58 (fit 1.00, arc 105 deg, ratio 0.40, spill 0.00, closed ring) x dark 0.92 x size 1.00; wheel 0.53 = shape 0.62 (fit 1.00, arc 111 deg, ratio 0.84, spill 0.00, closed ring) x dark 0.73 x size 1.00 | verify the best two | 6569 |
| verify | car wheel 2 | muse-spark-open | crop [754,232,1489,967] px with 2 outlines A and B | chose B, shape 0.25 (fit 0.70, arc 90 deg, ratio 0.54, spill 0.00, closed ring), edge ratio 1.00, box agreement 0.11, is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Both outlines include bodywork and side panel, B is smaller but still not tyre-only so depth would be wrong. | refine with box [978,625,1085,713] px + points [988,677], [1024,633], [1061,677], [1037,633] - points [1033,677], [1033,596], [989,732] | 15799 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region aligns with turbulent floodwater in foreground around the police vehicle and motorcycle. | accept | 18371 |
| segment | car wheel 2 | sam-2.1-hiera-small | box [978,625,1085,713] px + points [988,677], [1024,633], [1061,677], [1037,633] - points [1033,677], [1033,596], [989,732] | 6 proposals, 6 distinct; best shapes 0.69, 0.51 | verify the best two | 1222 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [245,432,580,768] px with 1 outline | chose NONE, is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Magenta outline encloses engine/exhaust/frame area between wheels, not the round outer edge of either tyre. | refine with box [245,538,307,645] px + points [248,593], [270,550], [297,593] - points [270,600], [273,573], [352,607] | 13830 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [245,538,307,645] px + points [248,593], [270,550], [297,593] - points [270,600], [273,573], [352,607] | 6 proposals, 5 distinct; best shapes 0.60, 0.43 | verify the best two | 1024 |
| verify | car wheel 2 | muse-spark-open | crop [857,497,1208,848] px with 2 outlines A and B | chose A, shape 0.69 (fit 0.94, arc 141 deg, ratio 0.87, spill 0.00, closed ring), edge ratio 0.77, box agreement 0.59, is_target=false, outline wrong, view side, edge visible 0.50, waterline visible; A is tighter than B but both dip into splash below waterline instead of straight waterline and do not follow tyre outer edge closely | refine with box [982,632,1076,701] px + points [986,674], [1006,639], [1052,639], [1072,674] - points [1027,674], [1027,716], [1032,616] | 11365 |
| segment | car wheel 2 | sam-2.1-hiera-small | box [982,632,1076,701] px + points [986,674], [1006,639], [1052,639], [1072,674] - points [1027,674], [1027,716], [1032,616] | 6 proposals, 6 distinct; best shapes 0.48, 0.45 | verify the best two | 1349 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [60,357,509,806] px with 2 outlines A and B | chose B, shape 0.43 (fit 0.78, arc 132 deg, ratio 0.51, spill 0.01, closed ring), edge ratio 0.96, not a tyre edge: 2 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.23, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B follows the round outer edge of the front tyre above the water with flat waterline at bottom, while A cuts across fork and misses tyre. | refine with box [170,561,278,678] px + points [181,635], [213,582], [253,590], [269,635] - points [222,626], [222,573], [231,546] | 17623 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [170,561,278,678] px + points [181,635], [213,582], [253,590], [269,635] - points [222,626], [222,573], [231,546] | 6 proposals, 6 distinct; best shapes 0.56, 0.41 | verify the best two | 1524 |
| verify | car wheel 2 | muse-spark-open | crop [806,464,1209,867] px with 2 outlines A and B | chose B, shape 0.45 (fit 0.90, arc 102 deg, ratio 0.67, spill 0.01, closed ring), edge ratio 0.87, not a tyre edge: 3 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.46, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B follows the round outer edge of the tyre above the water while A includes the splash; B is tight to the tyre with rim inside. | give up: the outline covers a different area than the tyre (0.46) | 23453 |
| verify | car wheel 3 | muse-spark-open | crop [1104,491,1429,816] px with 2 outlines A and B | chose A, shape 0.49 (fit 1.00, arc 96 deg, ratio 0.48, spill 0.02, closed ring), edge ratio 1.05, box agreement 0.54, is_target=true, outline good, view side, edge visible 0.55, waterline visible; A follows the tyre rubber above water with straight waterline while B includes splash water | refine with box [1229,587,1307,662] px + points [1237,636], [1258,603], [1288,610], [1296,636] - points [1266,639], [1266,678], [1266,584] | 55689 |
| segment | car wheel 3 | sam-2.1-hiera-small | box [1229,587,1307,662] px + points [1237,636], [1258,603], [1288,610], [1296,636] - points [1266,639], [1266,678], [1266,584] | 6 proposals, 6 distinct; best shapes 0.38, 0.18 | verify the best two | 1429 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [72,461,390,778] px with 2 outlines A and B | chose A, shape 0.56 (fit 0.92, arc 120 deg, ratio 0.74, spill 0.00, closed ring), edge ratio 1.03, box agreement 0.83, is_target=true, outline good, view side, edge visible 0.72, waterline visible; A follows the black tyre outer edge above water without including the silver mudguard while B rides up onto the mudguard | accept | 28166 |
| verify | car wheel 3 | muse-spark-open | crop [1089,408,1455,775] px with 2 outlines A and B | chose A, shape 0.38 (fit 1.00, arc 69 deg, ratio 0.86, spill 0.00, closed ring), edge ratio 0.98, box agreement 0.60, is_target=true, outline good, view side, edge visible 0.65, waterline visible; A follows the tyre's outer edge above water with straight waterline, B includes mudguard and bodywork | refine with box [1234,597,1318,661] px + points [1241,643], [1261,610], [1294,610], [1310,639] - points [1276,630], [1274,569], [1217,547] | 51327 |
| segment | car wheel 3 | sam-2.1-hiera-small | box [1234,597,1318,661] px + points [1241,643], [1261,610], [1294,610], [1310,639] - points [1276,630], [1274,569], [1217,547] | 6 proposals, 6 distinct; best shapes 0.43, 0.42 | verify the best two | 1523 |
| verify | car wheel 3 | muse-spark-open | crop [1146,501,1402,757] px with 2 outlines A and B | chose A, shape 0.43 (fit 1.00, arc 78 deg, ratio 0.59, spill 0.00, closed ring), edge ratio 1.02, box agreement 0.67, is_target=true, outline good, view side, edge visible 0.55, waterline visible; A follows the round outer edge of the black tyre above the water with rim inside, while B is a narrow sliver missing most of the tyre. | give up: the outline covers a different area than the tyre (0.67) | 40972 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 4997 px of water added next to the tyres | join to the verified water | 1259 |
| geometry | two wheeler wheel 1 | geometry | wheel and water masks | depth 15.3 cm, sigma 4.9 cm, 183 degrees of rim visible | candidate | 639 |
| estimate | photo | geometry | measurement 15.3 cm (sigma 5.0) and model estimate 32 cm | the estimate is 17 cm deeper, beyond the measurement's two-sigma bound of 25 cm | shallow reading not trusted: report the estimate | 0 |
| estimate | photo | muse-spark-open | measurement not used | 35 cm, sigma 20 cm | report model estimate | 0 |

### w_borivali_bus: An ST bus near the WEH flyover at Borivali (35520283).jpg (Commons, chosen for the wheel method)

Author Rakesh from Bangalore (Flickr), licence CC BY-SA 2.0 (https://creativecommons.org/licenses/by-sa/2.0/), source https://commons.wikimedia.org/wiki/File:An_ST_bus_near_the_WEH_flyover_at_Borivali_(35520283).jpg.

Result: **model_estimate**, depth 22.0 cm, sigma 20.0 cm, confidence 0.21.
Scene model's own estimate: 22.0 cm (sigma 20.0 cm), basis: shallow standing water filling ruts and covering road base around bus wheels.
Captured at (EXIF): 2005-07-28T13:58:55.

Reasons: standing water visible; bus wheel: water does not meet the wheel; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: shallow standing water filling ruts and covering road base around bus wheels.

Wall time 27.6 s (timings ms: scene 14, agent 27495, geometry 20); model cost USD 0.007509.

Overlay: `eval/out/wheel-final/w_borivali_bus_overlay.png`. Verifier images: `eval/out/wheel-final/w_borivali_bus_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1024x768 px (cached reading) | water visible; references: bus_wheel; own estimate 22 cm | search 1 vehicle box for wheels, check 1 wheel and segment the water | 14 |
| segment | water | sam-2.1-hiera-small | box [43,475,409,686] px + points [[191, 594]] | mask 49261 px (6% of photo) | verify | 821 |
| segment | bus 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [87,238,1024,684] px | 87 proposals from 24 grid points, 1 scene model wheel box and 3 dark blobs; best: wheel 0.62 = shape 0.63 (fit 0.83, arc 162 deg, ratio 0.97, spill 0.00, closed ring) x dark 0.97 x size 1.00; wheel 0.52 = shape 0.52 (fit 0.85, arc 129 deg, ratio 0.46, spill 0.00) x dark 1.00 x size 1.00 | verify the best two | 5547 |
| verify | bus 1 wheel | muse-spark-open | crop [262,87,1024,768] px with 2 outlines A and B | chose A, shape 0.63 (fit 0.83, arc 162 deg, ratio 0.97, spill 0.00, closed ring), edge ratio 1.13, box agreement 0.79, is_target=true, outline good, view side, edge visible 0.85, waterline not visible; A follows the round outer edge of the rear bus tyre above ground while B outlines empty ground under the body | accept | 13669 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region aligns with muddy standing puddle in foreground adjacent to bus. | accept | 26652 |
| geometry | bus wheel 1 | geometry | wheel and water masks | water does not meet the wheel | rejected | 18 |
| estimate | photo | muse-spark-open | no measurable wheel | 22 cm, sigma 20 cm | report model estimate | 0 |

### w_focus: Mehmet Abi, Allah Kolaylık Versin... - panoramio.jpg (Commons, chosen for the wheel method)

Author Hakan SAVAŞ78 (Panoramio), licence CC BY 3.0 (https://creativecommons.org/licenses/by/3.0/), source https://commons.wikimedia.org/wiki/File:Mehmet_Abi,_Allah_Kolayl%C4%B1k_Versin..._-_panoramio.jpg.

Result: **model_estimate**, depth 33.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 33.0 cm (sigma 20.0 cm), basis: water reaches lower third of car tyres across flooded street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car 1 wheel: the scene model did not confirm the mask (outline and the tyre's box disagree); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water reaches lower third of car tyres across flooded street.

Wall time 85.1 s (timings ms: scene 1, agent 85073, geometry 0); model cost USD 0.019632.

Overlay: `eval/out/wheel-final/w_focus_overlay.png`. Verifier images: `eval/out/wheel-final/w_focus_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 800x600 px (cached reading) | water visible; references: car_wheel, car_wheel; own estimate 33 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 1 |
| segment | water | sam-2.1-hiera-small | box [8,438,793,595] px + points [[124, 519]] | mask 117921 px (25% of photo) | verify | 1282 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [235,359,648,435] px | 93 proposals from 24 grid points, 2 scene model wheel boxes and 3 dark blobs; best: wheel 0.68 = shape 0.68 (fit 1.00, arc 126 deg, ratio 0.64, spill 0.01) x dark 1.00 x size 1.00; wheel 0.60 = shape 0.87 (fit 1.00, arc 156 deg, ratio 0.55, spill 0.00, closed ring) x dark 0.38 x size 1.00 | verify the best two | 4488 |
| verify | car 1 wheel | muse-spark-open | crop [158,262,414,518] px with 1 outline | chose NONE, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; Magenta outline follows the wheel arch/bodywork cutout, not the round outer edge of the tyre itself, so it encloses bodywork and empty space and would give wron | refine with box [264,386,310,422] px + points [268,409], [286,391], [305,409] - points [287,407], [287,384], [287,436] | 22169 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [264,386,310,422] px + points [268,409], [286,391], [305,409] - points [287,407], [287,384], [287,436] | 6 proposals, 5 distinct; best shapes 0.45, 0.39 | verify the best two | 1380 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the brown floodwater surface in the foreground contiguous with water around the submerged car. | accept | 46159 |
| verify | car 1 wheel | muse-spark-open | crop [163,276,419,532] px with 2 outlines A and B | chose A, shape 0.45 (fit 0.92, arc 96 deg, ratio 0.57, spill 0.00), edge ratio 1.71, not a tyre edge: 2 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.30, is_target=true, outline good, view side, edge visible 0.60, waterline visible; Outline A follows the black tyre's outer arc above the water closely with a straight waterline and without straying onto bodywork. | refine with box [254,372,303,408] px + points [256,395], [269,377], [290,377], [300,395] - points [278,391], [278,368], [256,410] | 24480 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [254,372,303,408] px + points [256,395], [269,377], [290,377], [300,395] - points [278,391], [278,368], [256,410] | 6 proposals, 5 distinct; best shapes 0.54, 0.40 | verify the best two | 1372 |
| verify | car 1 wheel | muse-spark-open | crop [154,262,410,518] px with 2 outlines A and B | chose B, shape 0.40 (fit 1.00, arc 72 deg, ratio 0.60, spill 0.00), edge ratio 4.85, box agreement 0.47, is_target=true, outline good, view side, edge visible 0.55, waterline visible; B tightly follows the black tyre's outer edge above the water while A strays onto the white mudguard/bodywork | give up: the outline covers a different area than the tyre (0.47) | 31110 |
| estimate | photo | muse-spark-open | no measurable wheel | 33 cm, sigma 20 cm | report model estimate | 0 |

### u_A-yqsrAME6g: Motorcyclist rides through flooded street during rain (Unsplash, chosen for the wheel method)

Author Aldward Castillo (@aldwardcv31), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/A-yqsrAME6g.

Result: **wheel_geometry**, depth 23.5 cm, sigma 5.9 cm, confidence 0.16.
Scene model's own estimate: 19.0 cm (sigma 20.0 cm), basis: Water covers lower quarter of motorcycle tyres across flooded carriageway.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two-wheeler wheel found; wheel size unknown, generic prior used; scene model waterline agrees within 0.6 cm.

Wall time 60.0 s (timings ms: scene 1, agent 59149, geometry 755); model cost USD 0.021228.

Overlay: `eval/out/wheel-final/u_A-yqsrAME6g_overlay.png`. Verifier images: `eval/out/wheel-final/u_A-yqsrAME6g_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 19 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 1 |
| segment | water | sam-2.1-hiera-small | box [928,626,1428,843] px + points [[1187, 733]] | mask 98469 px (6% of photo) | verify | 1210 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [403,531,867,750] px | 96 proposals from 24 grid points, 2 scene model wheel boxes and 4 dark blobs; best: wheel 0.66 = shape 0.80 (fit 0.94, arc 162 deg, ratio 0.58, spill 0.00, closed ring) x dark 0.65 x size 1.00; wheel 0.47 = shape 0.57 (fit 1.00, arc 102 deg, ratio 0.79, spill 0.00, closed ring) x dark 0.65 x size 1.00 | verify the best two | 4713 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [523,405,949,832] px with 2 outlines A and B | chose B, shape 0.57 (fit 1.00, arc 102 deg, ratio 0.79, spill 0.00, closed ring), edge ratio 3.94, not a tyre edge: 2 of 3 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.39, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; A includes swingarm chain and exhaust, B is only a small cap on the rear fender/tyre top and misses most of the tyre so neither follows the full tyre above wate | refine with box [683,561,823,648] px + points [698,621], [755,569], [808,621] - points [757,629], [781,569], [781,633] | 34846 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but offset to the right and misses the flooded area splashing around the motorcycle. | refine with box [288,640,832,790] px + point [560, 715] | 39563 |
| segment | water | sam-2.1-hiera-small | box [288,640,832,790] px + points [[560, 715]] | mask 44699 px (3% of photo) | verify | 284 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [683,561,823,648] px + points [698,621], [755,569], [808,621] - points [757,629], [781,569], [781,633] | 6 proposals, 5 distinct; best shapes 0.47, 0.36 | verify the best two | 1515 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the flooded water surface around the motorcycle wheels and covers the immediate water area. | accept | 9530 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [549,418,945,813] px with 2 outlines A and B | chose B, shape 0.36 (fit 0.84, arc 93 deg, ratio 0.74, spill 0.00, closed ring), edge ratio 1.50, box agreement 0.73, is_target=true, outline good, view side, edge visible 0.60, waterline visible; B follows the round outer edge of the rear tyre above the water with rim inside, while A is only a small fragment of the tyre edge. | accept | 16760 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 397 px of water added next to the tyres | join to the verified water | 1151 |
| geometry | two wheeler wheel 1 | geometry | wheel and water masks | depth 23.5 cm, sigma 5.9 cm, 180 degrees of rim visible | candidate | 754 |
| estimate | photo | geometry | measurement 23.5 cm and model estimate 20 cm | difference 3 cm, two combined sigma 42 cm | use the measurement | 0 |

### u_w8MXURXvqjQ: Man rides motorcycle through flooded street (Unsplash, chosen for the wheel method)

Author Aldward Castillo (@aldwardcv31), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/w8MXURXvqjQ.

Result: **model_estimate**, depth 23.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 23.0 cm (sigma 20.0 cm), basis: water reaches lower third of motorcycle wheels across flooded street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (Both outlines include the silver mudguard above the tyre instead of following the black tyre edge only.); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water reaches lower third of motorcycle wheels across flooded street.

Wall time 61.4 s (timings ms: scene 2, agent 61291, geometry 0); model cost USD 0.022775.

Overlay: `eval/out/wheel-final/u_w8MXURXvqjQ_overlay.png`. Verifier images: `eval/out/wheel-final/u_w8MXURXvqjQ_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 23 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [48,854,385,1036] px + points [[208, 939]] | mask 64503 px (4% of photo) | verify | 809 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [364,617,1157,844] px | 96 proposals from 24 grid points, 2 scene model wheel boxes and 4 dark blobs; best: wheel 0.75 = shape 1.00 (fit 1.00, arc 186 deg, ratio 0.95, spill 0.00, closed ring) x dark 0.49 x size 1.00; wheel 0.52 = shape 0.61 (fit 0.95, arc 120 deg, ratio 0.91, spill 0.00, closed ring) x dark 0.72 x size 1.00 | verify the best two | 5180 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but small corner far from motorcycle, missing floodwater surrounding the rider. | refine with box [992,694,1440,907] px + point [1200, 800] | 8770 |
| segment | water | sam-2.1-hiera-small | box [992,694,1440,907] px + points [[1200, 800]] | mask 90162 px (5% of photo) | verify | 1141 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [229,430,798,1000] px with 1 outline | chose NONE, is_target=false, outline wrong, view side, edge visible 0.75, waterline visible; Magenta outline encloses the chrome mudguard and fork along with the tyre instead of following the tyre outer edge only. | refine with box [403,649,590,798] px + points [410,738], [482,655], [556,661], [582,738] - points [496,749], [505,675], [539,686] | 7010 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [403,649,590,798] px + points [410,738], [482,655], [556,661], [582,738] - points [496,749], [505,675], [539,686] | 6 proposals, 6 distinct; best shapes 0.74, 0.38 | verify the best two | 1303 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only a small patch behind the bike and misses the flooded water around the front wheel and foreground. | refine with box [48,747,1040,1024] px + point [480, 907] | 8213 |
| segment | water | sam-2.1-hiera-small | box [48,747,1040,1024] px + points [[480, 907]] | mask 298394 px (17% of photo) | verify | 1156 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region aligns with flooded street water surrounding the motorcycle and rider. | accept | 10621 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [253,474,740,960] px with 2 outlines A and B | chose A, shape 0.74 (fit 0.92, arc 159 deg, ratio 0.84, spill 0.00, closed ring), edge ratio 3.26, not a tyre edge: the outline is an oval tilted 39 degrees: a cut or partial outline, not an upright wheel, box agreement 0.59, is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Magenta outline follows the silver mudguard at the top instead of the black tyre outer edge, adding non-tyre area. | refine with box [405,637,591,760] px + points [411,715], [470,647], [555,651], [579,710] - points [496,727], [496,639], [535,710] | 34782 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [405,637,591,760] px + points [411,715], [470,647], [555,651], [579,710] - points [496,727], [496,639], [535,710] | 6 proposals, 5 distinct; best shapes 0.58, 0.54 | verify the best two | 1433 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [228,422,800,994] px with 2 outlines A and B | chose A, shape 0.58 (fit 0.96, arc 129 deg, ratio 0.91, spill 0.03, closed ring), edge ratio 0.99, box agreement 0.72, is_target=false, outline wrong, view side, edge visible 0.75, waterline visible; Both outlines include the silver mudguard above the tyre instead of following the black tyre edge only. | give up | 11417 |
| estimate | photo | muse-spark-open | no measurable wheel | 23 cm, sigma 20 cm | report model estimate | 0 |

### u_qPaVJsINuQY: Person fixing motorcycle tire in floodwater (Unsplash, chosen for the wheel method)

Author Aldward Castillo (@aldwardcv31), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/qPaVJsINuQY.

Result: **model_estimate**, depth 10.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 10.0 cm (sigma 20.0 cm), basis: Water covers lower tyre and ankles across flooded carriageway.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (outline follows the rim, inside the tyre); person 1: the person is not standing upright (bending); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers lower tyre and ankles across flooded carriageway.

Wall time 78.8 s (timings ms: scene 1, agent 78611, geometry 0); model cost USD 0.019913.

Overlay: `eval/out/wheel-final/u_qPaVJsINuQY_overlay.png`. Verifier images: `eval/out/wheel-final/u_qPaVJsINuQY_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, person; own estimate 10 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 1 |
| segment | water | sam-2.1-hiera-small | box [48,747,481,1036] px + points [[240, 907]] | mask 162422 px (10% of photo) | verify | 2033 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [569,338,1368,789] px | 96 proposals from 24 grid points, 2 scene model wheel boxes and 4 dark blobs; best: wheel 0.35 = shape 0.65 (fit 0.93, arc 165 deg, ratio 0.38, spill 0.04, closed ring) x dark 0.09 x size 1.00; wheel 0.35 = shape 0.44 (fit 0.84, arc 114 deg, ratio 0.56, spill 0.00, closed ring) x dark 0.58 x size 1.00 | verify the best two | 7161 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but tiny and misses the flooded area around the person and motorcycle. | refine with box [832,555,1472,939] px + point [1200, 694] | 8650 |
| segment | water | sam-2.1-hiera-small | box [832,555,1472,939] px + points [[1200, 694]] | mask 161028 px (9% of photo) | verify | 1144 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [249,0,1452,1067] px with 2 outlines A and B | chose B, shape 0.44 (fit 0.84, arc 114 deg, ratio 0.56, spill 0.00, closed ring), edge ratio 0.62, box agreement 0.81, is_target=false, outline partial, view side, edge visible 0.65, waterline visible; B is on the rear wheel but its outline bulges to include the exhaust and suspension instead of following the tyre edge alone. | refine with box [920,408,1069,681] px + points [929,619], [983,432], [1049,512], [1055,619] - points [995,582], [1019,555], [989,517] | 9179 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [920,408,1069,681] px + points [929,619], [983,432], [1049,512], [1055,619] - points [995,582], [1019,555], [989,517] | 6 proposals, 5 distinct; best shapes 0.33, 0.32 | verify the best two | 1282 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the flooded street surface around the motorcycle and person. | accept | 7357 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [573,100,1423,951] px with 2 outlines A and B | chose B, shape 0.32 (fit 0.75, arc 102 deg, ratio 0.44, spill 0.00, closed ring), edge ratio 1.06, box agreement 0.48, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B tightly follows the rear tyre outer edge above water while A includes exhaust and bodywork | refine with box [900,462,1002,713] px + points [909,628], [926,508], [977,526], [985,628] - points [947,632], [1015,632], [938,483] | 16671 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [900,462,1002,713] px + points [909,628], [926,508], [977,526], [985,628] - points [947,632], [1015,632], [938,483] | 6 proposals, 5 distinct; best shapes 0.60, 0.57 | verify the best two | 1413 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [615,250,1283,919] px with 2 outlines A and B | chose A, shape 0.60 (fit 0.90, arc 135 deg, ratio 0.67, spill 0.00, closed ring), edge ratio 0.59, not a tyre edge: a darker ring lies just outside the outline (dark 0.53 outside, 0.19 inside): it follows the rim, inside the tyre, box agreement 0.75, is_target=true, outline good, view side, edge visible 0.82, waterline visible; A follows the outer black tyre edge above the water with rim inside and straight waterline, B is only a small sliver on the tyre side. | give up: the outline sits inside the tyre (darker outside than inside, 0.59) | 42694 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points (segmented during the checks) | 1 masks | measure each against the stature prior | 1231 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 3390 |
| geometry | person 1 | geometry | person mask | the person is not standing upright (bending) | declined | 0 |
| estimate | photo | muse-spark-open | no measurable wheel | 10 cm, sigma 20 cm | report model estimate | 0 |

### u_ic8V3A59gpg: Car driving through floodwater splashes (Unsplash, chosen for the wheel method)

Author Aldward Castillo (@aldwardcv31), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/ic8V3A59gpg.

Result: **model_estimate**, depth 22.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 22.0 cm (sigma 20.0 cm), basis: Waterline on front tyre shows shallow street flooding covering lower tyre.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car wheel found; wheel size unknown, generic prior used; scene model waterline agrees within 1.3 cm; the car wheel measured 2.7 cm (sigma 1.7 cm), but the scene model estimates 22 cm at that wheel, above the measurement's two-sigma bound; a shallow reading that disagrees is not trusted, so the scene model's estimate is reported; estimate based on: Waterline on front tyre shows shallow street flooding covering lower tyre.

Wall time 22.0 s (timings ms: scene 2, agent 19652, geometry 2282); model cost USD 0.009587.

Overlay: `eval/out/wheel-final/u_ic8V3A59gpg_overlay.png`. Verifier images: `eval/out/wheel-final/u_ic8V3A59gpg_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: car_wheel; own estimate 22 cm | search 1 vehicle box for wheels, check 1 wheel and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [32,619,993,1063] px + points [[512, 875]] | mask 11325 px (1% of photo) | verify | 1227 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [0,0,1600,670] px | 87 proposals from 24 grid points, 1 scene model wheel box and 3 dark blobs; best: wheel 0.50 = shape 0.63 (fit 0.87, arc 156 deg, ratio 0.79, spill 0.01, closed ring) x dark 0.58 x size 1.00; wheel 0.27 = shape 0.60 (fit 0.95, arc 120 deg, ratio 0.31, spill 0.00, closed ring) x dark 0.48 x size 0.61 | verify the best two | 8735 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is on water but is a tiny fragment missing most floodwater around the vehicle tire. | refine with box [29,619,995,1051] px + point [499, 809] | 9256 |
| segment | water | sam-2.1-hiera-small | box [29,619,995,1051] px + points [[499, 809]] | mask 462715 px (27% of photo) | verify | 611 |
| verify | car 1 wheel | muse-spark-open | crop [0,0,1290,1067] px with 2 outlines A and B | chose A, shape 0.63 (fit 0.87, arc 156 deg, ratio 0.79, spill 0.01, closed ring), edge ratio 1.81, box agreement 0.77, is_target=true, outline good, view side, edge visible 0.65, waterline visible; A follows the tyre outer edge above the water with rim inside and straight waterline, B is only a tiny rim fragment. | accept | 9845 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies entirely on the turbulent floodwater surface surrounding the vehicle tire. | accept | 7854 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 34078 px of water added next to the tyres | join to the verified water | 610 |
| geometry | car wheel 1 | geometry | wheel and water masks | depth 2.7 cm, sigma 1.1 cm, 285 degrees of rim visible | candidate | 2281 |
| estimate | photo | geometry | measurement 2.7 cm (sigma 1.7) and model estimate 22 cm | the estimate is 19 cm deeper, beyond the measurement's two-sigma bound of 6 cm | shallow reading not trusted: report the estimate | 0 |
| estimate | photo | muse-spark-open | measurement not used | 22 cm, sigma 20 cm | report model estimate | 0 |

### u_DhM9aChufzU: Cars driving through flooded street after heavy rain (Unsplash, chosen for the wheel method)

Author Aldward Castillo (@aldwardcv31), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/DhM9aChufzU.

Result: **model_estimate**, depth 33.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 33.0 cm (sigma 20.0 cm), basis: Floodwater covers road and reaches mid-tyre of sedan in foreground.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car 1 wheel: the scene model did not confirm the mask (No car wheel/tyre is visible in this crop; both magenta outlines cover flooded water/ground behind the car, not a tyre, so neither follows a tyre edge.); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Floodwater covers road and reaches mid-tyre of sedan in foreground.

Wall time 42.8 s (timings ms: scene 3, agent 42750, geometry 0); model cost USD 0.020602.

Overlay: `eval/out/wheel-final/u_DhM9aChufzU_overlay.png`. Verifier images: `eval/out/wheel-final/u_DhM9aChufzU_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: car_wheel, car_wheel; own estimate 33 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 3 |
| segment | water | sam-2.1-hiera-small | box [8,674,1598,1065] px + points [[243, 878]] | mask 402836 px (24% of photo) | verify | 1197 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [284,461,1297,710] px | 90 proposals from 24 grid points, 2 scene model wheel boxes and 2 dark blobs; best: wheel 0.46 = shape 0.52 (fit 1.00, arc 93 deg, ratio 0.83, spill 0.00, closed ring) x dark 0.78 x size 1.00; wheel 0.43 = shape 0.66 (fit 0.96, arc 138 deg, ratio 0.30, spill 0.01, closed ring) x dark 0.30 x size 1.00 | verify the best two | 4885 |
| verify | car 1 wheel | muse-spark-open | crop [1098,380,1389,672] px with 1 outline | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Magenta outline encloses flooded water/road surface beside car bumper, not any tyre or wheel. | refine with box [1190,470,1297,582] px - points [1229,526], [1244,555], [1214,497] | 6406 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [1190,470,1297,582] px - points [1229,526], [1244,555], [1214,497] | 3 proposals, 3 distinct; best shapes 0.41, 0.17 | verify the best two | 1394 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but only covers foreground and misses the water surrounding the stranded silver car. | refine with box [192,598,560,726] px + point [320, 662] | 20240 |
| segment | water | sam-2.1-hiera-small | box [192,598,560,726] px + points [[320, 662]] | mask 42143 px (2% of photo) | verify | 1218 |
| verify | car 1 wheel | muse-spark-open | crop [1080,366,1418,704] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; No car wheel/tyre is visible above water in the yellow box crop; both magenta outlines trace floodwater and splash beside the rear bumper, not a tyre edge. | refine with box [1190,470,1297,582] px - points [1242,552], [1242,586], [1283,569] | 12703 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [1190,470,1297,582] px - points [1242,552], [1242,586], [1283,569] | 3 proposals, 3 distinct; best shapes 0.44, 0.23 | verify the best two | 599 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta patch lies on the flooded street water directly beside the silver car’s front wheel. | accept | 13420 |
| verify | car 1 wheel | muse-spark-open | crop [1079,367,1415,702] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; No car wheel/tyre is visible in this crop; both magenta outlines cover flooded water/ground behind the car, not a tyre, so neither follows a tyre edge. | give up | 16681 |
| estimate | photo | muse-spark-open | no measurable wheel | 33 cm, sigma 20 cm | report model estimate | 0 |

### u_lIkOxcck448: Motorcycle rider on flooded street (Unsplash, chosen for the wheel method)

Author Aldward Castillo (@aldwardcv31), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/lIkOxcck448.

Result: **model_estimate**, depth 28.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 28.0 cm (sigma 20.0 cm), basis: water covers road and reaches lower third to hub of motorcycle wheels.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (outline and the tyre's box disagree); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: water covers road and reaches lower third to hub of motorcycle wheels.

Wall time 64.4 s (timings ms: scene 2, agent 64314, geometry 0); model cost USD 0.022055.

Overlay: `eval/out/wheel-final/u_lIkOxcck448_overlay.png`. Verifier images: `eval/out/wheel-final/u_lIkOxcck448_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel; own estimate 28 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [72,702,798,1064] px + points [[354, 878]] | mask 284033 px (17% of photo) | verify | 1246 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [790,568,1316,769] px | 96 proposals from 24 grid points, 2 scene model wheel boxes and 4 dark blobs; best: wheel 0.47 = shape 0.52 (fit 0.84, arc 132 deg, ratio 0.68, spill 0.00, closed ring) x dark 0.82 x size 1.00; wheel 0.24 = shape 0.36 (fit 0.98, arc 69 deg, ratio 0.62, spill 0.00, closed ring) x dark 0.31 x size 1.00 | verify the best two | 5256 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but in foreground corner and misses the flooded area directly around the motorcycle. | refine with box [512,555,1408,939] px + point [1088, 726] | 9859 |
| segment | water | sam-2.1-hiera-small | box [512,555,1408,939] px + points [[1088, 726]] | mask 248309 px (15% of photo) | verify | 551 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [812,277,1540,1005] px with 2 outlines A and B | chose A, shape 0.52 (fit 0.84, arc 132 deg, ratio 0.68, spill 0.00, closed ring), edge ratio 2.06, not a tyre edge: 4 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.40, is_target=true, outline good, view side, edge visible 0.65, waterline visible; Outline A tightly follows the rear tyre's outer edge above the water with straight waterline, while B is off the bike in the water. | refine with box [1036,561,1201,674] px + points [1041,637], [1085,575], [1158,577], [1192,630] - points [1118,630], [1092,608], [1180,565] | 11337 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1036,561,1201,674] px + points [1041,637], [1085,575], [1158,577], [1192,630] - points [1118,630], [1092,608], [1180,565] | 6 proposals, 5 distinct; best shapes 0.23, 0.20 | verify the best two | 1270 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region covers the flooded water surface and splash around the motorcycle. | accept | 19488 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [878,373,1372,867] px with 2 outlines A and B | chose B, shape 0.20 (fit 0.79, arc 57 deg, ratio 0.73, spill 0.00, closed ring), edge ratio 0.25, not a tyre edge: 2 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.30, is_target=false, outline partial, view side, edge visible 0.60, waterline visible; B follows the rear tyre but its outline also encloses the shock absorber and side bodywork above the tyre. | refine with box [1043,622,1197,734] px + points [1053,699], [1090,642], [1157,640], [1187,694] - points [1118,687], [1068,667], [1098,622] | 33771 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1043,622,1197,734] px + points [1053,699], [1090,642], [1157,640], [1187,694] - points [1118,687], [1068,667], [1098,622] | 6 proposals, 5 distinct; best shapes 0.62, 0.44 | verify the best two | 1477 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [865,422,1366,923] px with 2 outlines A and B | chose A, shape 0.62 (fit 0.83, arc 165 deg, ratio 0.69, spill 0.00, closed ring), edge ratio 1.80, box agreement 0.69, is_target=true, outline good, view side, edge visible 0.55, waterline visible; A tracks the black tyre outer edge above water tightly, B bulges left onto the shock spring. | give up: the outline covers a different area than the tyre (0.69) | 11000 |
| estimate | photo | muse-spark-open | no measurable wheel | 28 cm, sigma 20 cm | report model estimate | 0 |

### u_P7Z3HwNWPeQ: People riding bicycles through a flooded street (Unsplash, chosen for the wheel method)

Author Dibakar Roy (@dibakar16roy), licence Unsplash License (https://unsplash.com/license), source https://unsplash.com/photos/P7Z3HwNWPeQ.

Result: **model_estimate**, depth 27.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 27.0 cm (sigma 20.0 cm), basis: Water reaches mid-tyre to hub on multiple bikes and rickshaws across road.
Captured at (EXIF): not recorded.

Reasons: standing water visible; no wheel size is known for a other; two-wheeler wheel found; wheel size unknown, generic prior used; scene model waterline agrees within 8.5 cm; two wheeler 1 wheel: the scene model did not confirm the mask (Neither magenta outline follows the front tyre; A covers seat/engine and B is a tiny engine patch, both miss the tyre's outer edge entirely.); two wheeler 2 wheel: the scene model did not confirm the mask (B follows the right sidewall of the rear tyre while A traces splash water, but B covers only part of the tyre so it is partial not good.); the two-wheeler wheel measured 2.5 cm (sigma 8.7 cm), but the scene model estimates 26 cm at that wheel, above the measurement's two-sigma bound; a shallow reading that disagrees is not trusted, so the scene model's estimate is reported; estimate based on: Water reaches mid-tyre to hub on multiple bikes and rickshaws across road.

Wall time 79.1 s (timings ms: scene 2, agent 78226, geometry 815); model cost USD 0.04898.

Overlay: `eval/out/wheel-final/u_P7Z3HwNWPeQ_overlay.png`. Verifier images: `eval/out/wheel-final/u_P7Z3HwNWPeQ_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel; own estimate 27 cm | search 2 vehicle boxes for wheels, check 5 wheels and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [8,760,638,1065] px + points [[237, 920]] | mask 153548 px (9% of photo) | verify | 1160 |
| segment | two wheeler wheel 3 | sam-2.1-hiera-small | box [280,522,436,656] px + points [291,568], [355,525], [419,568] - points [360,585], [440,606] | 6 proposals, 6 distinct; best shapes 0.44, 0.41 | verify the best two | 2889 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [227,386,292,478] px | 89 proposals from 24 grid points, 1 scene model wheel box and 4 dark blobs; best: wheel 0.09 = shape 0.21 (fit 0.95, arc 45 deg, ratio 0.65, spill 0.01) x dark 0.58 x size 0.53; wheel 0.01 = shape 0.01 (fit 0.64, arc 42 deg, ratio 0.60, spill 0.18) x dark 0.32 x size 0.88 | verify the best two | 4636 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [613,514,1286,946] px | 90 proposals from 24 grid points, 3 scene model wheel boxes and 0 dark blobs; best: wheel 0.44 = shape 0.52 (fit 0.89, arc 117 deg, ratio 0.73, spill 0.00, closed ring) x dark 0.70 x size 1.00; wheel 0.33 = shape 0.37 (fit 0.62, arc 177 deg, ratio 0.90, spill 0.01) x dark 0.76 x size 1.00 | verify the best two | 8646 |
| verify | two wheeler wheel 3 | muse-spark-open | crop [155,385,561,791] px with 2 outlines A and B | chose B, shape 0.41 (fit 0.64, arc 231 deg, ratio 0.92, spill 0.00), edge ratio 1.64, box agreement 0.84, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B follows the tyre outer edge above water while A cuts across the wheel, so B is the correct tyre outline. | accept | 17752 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [128,303,384,559] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.15, waterline visible; Both magenta outlines trace the motorcycle tail/license area not the tyre, the only visible tyre is the bicycle wheel at bottom right largely submerged. | refine with box [241,407,272,431] px + points [293,537], [320,550], [360,547] - points [346,553], [256,431], [256,418] | 20569 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [241,407,272,431] px + points [293,537], [320,550], [360,547] - points [346,553], [256,431], [256,418] | 6 proposals, 5 distinct; best shapes 0.20, 0.00 | verify the best two | 1397 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [529,214,1579,1067] px with 2 outlines A and B | chose B, shape 0.37 (fit 0.62, arc 177 deg, ratio 0.90, spill 0.01), edge ratio 1.06, not a tyre edge: 2 of 4 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.22, is_target=true, outline good, view side, edge visible 0.60, waterline visible; B follows the rear tyre's outer edge above water with flat waterline, A outlines engine and leg not a tyre | refine with box [1014,605,1235,753] px + points [1031,704], [1101,619], [1185,636], [1217,704] - points [1122,713], [1117,602], [1038,739] | 22559 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1014,605,1235,753] px + points [1031,704], [1101,619], [1185,636], [1217,704] - points [1122,713], [1117,602], [1038,739] | 6 proposals, 5 distinct; best shapes 0.45, 0.38 | verify the best two | 1500 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only a small foreground patch missing the flooded area around riders and rickshaws. | refine with box [560,694,1600,1062] px + point [1200, 907] | 32187 |
| segment | water | sam-2.1-hiera-small | box [560,694,1600,1062] px + points [[1200, 907]] | mask 200119 px (12% of photo) | verify | 848 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [130,296,386,552] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.35, waterline visible; Neither outline follows the rear tyre; A traces bodywork beside the tail light and B is a tiny fragment, both miss the tyre's outer edge above the water. | refine with box [239,452,282,488] px + points [241,480], [249,462], [270,462], [278,479] - points [259,473], [259,450], [240,470] | 8709 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [239,452,282,488] px + points [241,480], [249,462], [270,462], [278,479] - points [259,473], [259,450], [240,470] | 6 proposals, 5 distinct; best shapes 0.39, 0.24 | verify the best two | 1062 |
| validate | two wheeler 1 wheel | validator | answer to verify two wheeler 1 wheel | tyre_box: [[240, 620], [540, 850]] is not a box [x_min, y_min, x_max, y_max]; dropped; corrected_box: [[240, 620], [540, 850]] is not a box [x_min, y_min, x_max, y_max]; dropped | drop 2 fields that failed, use the rest | 0 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [759,313,1481,1036] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.35, waterline visible; Neither magenta outline follows the rear tyre; A is a tiny sliver on bodywork and B encloses seat and side panel, both far from the wheel's outer edge above wat | refine with box [1014,605,1235,753] px + points [965,841], [1012,790], [1084,797], [1120,841] - points [1041,855], [1033,732], [1134,783] | 11253 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1014,605,1235,753] px + points [965,841], [1012,790], [1084,797], [1120,841] - points [1041,855], [1033,732], [1134,783] | 6 proposals, 5 distinct; best shapes 0.67, 0.52 | verify the best two | 606 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [132,350,388,606] px with 2 outlines A and B | chose B, shape 0.24 (fit 0.94, arc 48 deg, ratio 0.52, spill 0.00), edge ratio 0.61, not a tyre edge: 2 of 3 points the scene model put on the tyre rubber lie outside the outline: it sits inside the tyre, box agreement 0.18, is_target=false, outline partial, view head_on, edge visible 0.55, waterline visible; B follows the right sidewall of the rear tyre while A traces splash water, but B covers only part of the tyre so it is partial not good. | give up | 31207 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only a small splash patch around foreground bike, missing flooded street around other riders. | refine with box [8,555,1600,1062] px + point [227, 809] | 33575 |
| segment | water | sam-2.1-hiera-small | box [8,555,1600,1062] px + points [[227, 809]] | mask 546123 px (32% of photo) | verify | 484 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [700,336,1490,1067] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.65, waterline visible; Neither magenta outline follows the front tyre; A covers seat/engine and B is a tiny engine patch, both miss the tyre's outer edge entirely. | give up | 24733 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta overlay covers the flooded street water around the motorbike and bicycles in foreground. | accept | 8702 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 1572 px of water added next to the tyres | join to the verified water | 1117 |
| geometry | two wheeler wheel 3 | geometry | wheel and water masks | depth 2.5 cm, sigma 2.0 cm, 240 degrees of rim visible | candidate | 814 |
| estimate | photo | geometry | measurement 2.5 cm (sigma 8.7) and model estimate 26 cm | the estimate is 23 cm deeper, beyond the measurement's two-sigma bound of 20 cm | shallow reading not trusted: report the estimate | 0 |
| estimate | photo | muse-spark-open | measurement not used | 27 cm, sigma 20 cm | report model estimate | 0 |

### p_23939154: Motorcyclists navigate through a flooded street in Kolkata, India (Pexels, chosen for the wheel method)

Author Dibakar Roy (Pexels, dibakar-roy-2432543), licence Pexels License (https://www.pexels.com/license/), source https://www.pexels.com/photo/men-riding-motorcycles-on-a-flooded-street-23939154/.

Result: **model_estimate**, depth 27.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 27.0 cm (sigma 20.0 cm), basis: Water covers lower third of motorcycle wheels and shins of standing person.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (outline is not the tyre's outer edge); two wheeler 2 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); person 1: the outline does not match the person the scene model boxed; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers lower third of motorcycle wheels and shins of standing person.

Wall time 50.9 s (timings ms: scene 4, agent 50763, geometry 0); model cost USD 0.030155.

Overlay: `eval/out/wheel-final/p_23939154_overlay.png`. Verifier images: `eval/out/wheel-final/p_23939154_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, two_wheeler_wheel, person; own estimate 27 cm | search 2 vehicle boxes for wheels, check 3 wheels and segment the water | 4 |
| segment | water | sam-2.1-hiera-small | box [688,800,993,1036] px + points [[832, 923]] | mask 35029 px (2% of photo) | verify | 1667 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [71,417,257,623] px | 78 proposals from 24 grid points, 1 scene model wheel box and 0 dark blobs; best: wheel 0.33 = shape 0.35 (fit 1.00, arc 63 deg, ratio 0.62, spill 0.00) x dark 0.90 x size 1.00; wheel 0.30 = shape 0.45 (fit 1.00, arc 84 deg, ratio 0.33, spill 0.01) x dark 0.33 x size 1.00 | verify the best two | 5353 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [268,557,1245,950] px | 96 proposals from 24 grid points, 2 scene model wheel boxes and 4 dark blobs; best: wheel 0.58 = shape 0.69 (fit 0.99, arc 132 deg, ratio 0.77, spill 0.01, closed ring) x dark 0.69 x size 1.00; wheel 0.58 = shape 0.65 (fit 1.00, arc 117 deg, ratio 0.75, spill 0.00, closed ring) x dark 0.78 x size 1.00 | verify the best two | 10191 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but tiny and does not cover the flooded street around the motorcycles. | refine with box [80,694,1520,1046] px + point [1200, 534] | 9731 |
| segment | water | sam-2.1-hiera-small | box [80,694,1520,1046] px + points [[1200, 534]] | mask 430095 px (25% of photo) | verify | 1167 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [13,331,348,666] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view head_on, edge visible 0.55, waterline visible; Both magenta outlines are in the water/reflection and do not follow the front tyre under the red mudguard; no outline traces the tyre's outer edge above water. | refine with box [108,437,155,475] px + points [115,467], [129,453], [145,467] - points [132,463], [130,426], [132,498] | 11474 |
| segment | two wheeler 2 wheel | sam-2.1-hiera-small | box [108,437,155,475] px + points [115,467], [129,453], [145,467] - points [132,463], [130,426], [132,498] | 6 proposals, 6 distinct; best shapes 0.63, 0.43 | verify the best two | 1307 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta overlay aligns with flooded street water surrounding the motorcycles and riders. | accept | 8637 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [141,395,720,975] px with 1 outline | chose NONE, is_target=false, outline wrong, view oblique, edge visible 0.60, waterline visible; Magenta outline encloses the front mudguard/fender together with the tyre instead of following the tyre outer edge only. | refine with box [320,642,543,769] px + points [335,711], [422,653], [497,665], [520,711] - points [428,723], [457,688], [433,642] | 12359 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [320,642,543,769] px + points [335,711], [422,653], [497,665], [520,711] - points [428,723], [457,688], [433,642] | 6 proposals, 5 distinct; best shapes 0.70, 0.55 | verify the best two | 1539 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [140,410,722,993] px with 2 outlines A and B | chose A, shape 0.70 (fit 0.92, arc 147 deg, ratio 0.54, spill 0.00, closed ring), edge ratio 1.79, not a tyre edge: the outline is an oval tilted 27 degrees: a cut or partial outline, not an upright wheel, box agreement 0.64, is_target=true, outline good, view side, edge visible 0.60, waterline visible; A follows the outer black tyre edge above water while B includes the mudguard and fork | refine with box [318,640,457,751] px + points [323,710], [370,649], [422,652], [448,704] - points [384,716], [478,704], [434,693] | 10086 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [318,640,457,751] px + points [323,710], [370,649], [422,652], [448,704] - points [384,716], [478,704], [434,693] | 6 proposals, 5 distinct; best shapes 0.53, 0.44 | verify the best two | 1336 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [137,398,725,986] px with 2 outlines A and B | chose A, shape 0.53 (fit 0.88, arc 129 deg, ratio 0.69, spill 0.01, closed ring), edge ratio 1.41, not a tyre edge: the outline is 17% of the vehicle's width; a two wheeler wheel is 18 to 70%, box agreement 0.90, is_target=true, outline good, view side, edge visible 0.45, waterline visible; A follows the tyre outer edge tightly above water while B includes the mudguard and fender giving wrong depth. | give up: the outline is 17% of the vehicle's width; a two wheeler wheel is 18 to 70% | 8783 |
| verify | two wheeler 2 wheel | muse-spark-open | crop [0,320,244,593] px with 2 outlines A and B | chose B, shape 0.43 (fit 0.71, arc 156 deg, ratio 0.93, spill 0.00), edge ratio 1.42, box agreement 0.49, is_target=true, outline good, view head_on, edge visible 0.55, waterline visible; B follows the narrow front tyre edge above water while A strays into water splash, B stays on tyre rubber for depth measurement. | give up: tyre seen head-on, its outline cannot be measured | 32514 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points (segmented during the checks) | 1 masks | measure each against the stature prior | 854 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 5981 |
| geometry | person 1 | geometry | person mask | the outline does not match the person the scene model boxed | declined | 2 |
| estimate | photo | muse-spark-open | no measurable wheel | 27 cm, sigma 20 cm | report model estimate | 0 |

### p_21961069: A man on a motorcycle struggles through a flooded street in Kolkata, India (Pexels, chosen for the wheel method)

Author Dibakar Roy (Pexels, dibakar-roy-2432543), licence Pexels License (https://www.pexels.com/license/), source https://www.pexels.com/photo/man-dragging-a-motorcycle-through-the-flooding-21961069/.

Result: **model_estimate**, depth 45.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 45.0 cm (sigma 20.0 cm), basis: Water submerges motorcycle up to seat/engine and reaches knee of standing adult..
Captured at (EXIF): not recorded.

Reasons: standing water visible; person 1: the person is not standing upright (bending); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water submerges motorcycle up to seat/engine and reaches knee of standing adult..

Wall time 30.3 s (timings ms: scene 2, agent 30196, geometry 0); model cost USD 0.009971.

Overlay: `eval/out/wheel-final/p_21961069_overlay.png`. Verifier images: `eval/out/wheel-final/p_21961069_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: person; own estimate 45 cm | no wheel: measure 1 person against the stature prior and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [35,286,638,654] px + points [[285, 440]] | mask 229992 px (13% of photo) | verify | 2094 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta region is water but is offset to the left and misses the flooded water immediately around the person and motorcycle. | refine with box [720,694,1520,1014] px + point [1120, 854] | 10430 |
| segment | water | sam-2.1-hiera-small | box [720,694,1520,1014] px + points [[1120, 854]] | mask 130656 px (8% of photo) | verify | 315 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only a tiny patch under the motorcycle, missing most floodwater around it. | refine with box [48,277,608,662] px + point [288, 448] | 8036 |
| segment | water | sam-2.1-hiera-small | box [48,277,608,662] px + points [[288, 448]] | mask 219119 px (13% of photo) | verify | 93 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is floodwater but is on the left background and misses the water immediately around the person and motorcycle. | accept (coverage partial) | 9072 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points (segmented during the checks) | 1 masks | measure each against the stature prior | 1234 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 1725 |
| geometry | person 1 | geometry | person mask | the person is not standing upright (bending) | declined | 0 |
| estimate | photo | muse-spark-open | no measurable wheel | 45 cm, sigma 20 cm | report model estimate | 0 |

### p_21960705: A motorcyclist in gear tackles urban flooding in Kolkata, India (Pexels, chosen for the wheel method)

Author Dibakar Roy (Pexels, dibakar-roy-2432543), licence Pexels License (https://www.pexels.com/license/), source https://www.pexels.com/photo/side-view-of-a-man-on-a-motorcycle-21960705/.

Result: **person_reference**, depth 6.9 cm, sigma 22.8 cm, confidence 0.09.
Scene model's own estimate: 32.0 cm (sigma 20.0 cm), basis: Motorcycle wheels half submerged in foreground, ankle to shin depth in background street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; two wheeler 1 wheel: the scene model did not confirm the mask (outline only partly right: B follows the rear tyre arc above water while A is only the mudguard, but B's top-left edge cuts slightly inside the tyre and includes swingarm area.); person 1: the shoulder line could not be found; person 2: the water just below was segmented locally, not checked by the scene model; a person standing in the water was measured against the adult stature prior (159 cm, sigma 9 cm); a person reference is coarse: expect about 10 to 15 cm of error; the scene model puts the water at the shin, about 25 cm (19 cm apart).

Wall time 55.5 s (timings ms: scene 1, agent 55377, geometry 0); model cost USD 0.029463.

Overlay: `eval/out/wheel-final/p_21960705_overlay.png`. Verifier images: `eval/out/wheel-final/p_21960705_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: two_wheeler_wheel, two_wheeler_wheel, person, person; own estimate 32 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 1 |
| segment | water | sam-2.1-hiera-small | box [32,512,513,1047] px + points [[240, 747]] | mask 276950 px (16% of photo) | verify | 2595 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [592,640,1364,849] px | 96 proposals from 24 grid points, 2 scene model wheel boxes and 4 dark blobs; best: wheel 0.42 = shape 0.47 (fit 0.87, arc 114 deg, ratio 0.37, spill 0.00, closed ring) x dark 0.79 x size 1.00; wheel 0.38 = shape 0.40 (fit 1.00, arc 72 deg, ratio 0.43, spill 0.00, closed ring) x dark 0.90 x size 1.00 | verify the best two | 8054 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta region is floodwater but is confined to lower-left corner and misses the water surrounding the motorcycle and riders in the center. | refine with box [992,555,1568,939] px + point [1248, 726] | 9790 |
| segment | water | sam-2.1-hiera-small | box [992,555,1568,939] px + points [[1248, 726]] | mask 34449 px (2% of photo) | verify | 350 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [741,360,1453,1067] px with 2 outlines A and B | chose A, shape 0.47 (fit 0.87, arc 114 deg, ratio 0.37, spill 0.00, closed ring), edge ratio 1.11, box agreement 0.53, is_target=false, outline wrong, view side, edge visible 0.50, waterline visible; A is nearer the rear tyre but includes suspension and misses the upper tyre arc, so it does not follow the tyre outer edge. | refine with box [1093,653,1250,777] px + points [1108,735], [1161,660], [1222,735], [1214,756] - points [1168,738], [1140,724], [1204,650] | 11546 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1093,653,1250,777] px + points [1108,735], [1161,660], [1222,735], [1214,756] - points [1168,738], [1140,724], [1204,650] | 6 proposals, 6 distinct; best shapes 0.67, 0.49 | verify the best two | 1374 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [831,403,1458,1030] px with 2 outlines A and B | chose B, shape 0.49 (fit 0.91, arc 108 deg, ratio 0.53, spill 0.00, closed ring), edge ratio 0.58, box agreement 0.37, is_target=false, outline partial, view side, edge visible 0.65, waterline visible; A is only the shock spring, B is the rear wheel but its left edge bulges to include the suspension and bodywork instead of following the tyre's outer edge. | refine with box [1123,663,1251,764] px + points [1135,745], [1173,676], [1223,682], [1239,732] - points [1185,720], [1141,726], [1173,657] | 10162 |
| segment | two wheeler 1 wheel | sam-2.1-hiera-small | box [1123,663,1251,764] px + points [1135,745], [1173,676], [1223,682], [1239,732] - points [1185,720], [1141,726], [1173,657] | 6 proposals, 6 distinct; best shapes 0.60, 0.25 | verify the best two | 1417 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=false, coverage wrong; Magenta region is the motorcycle rear, not the floodwater surface. | refine with box [80,747,560,1014] px + point [320, 907] | 26597 |
| segment | water | sam-2.1-hiera-small | box [80,747,560,1014] px + points [[320, 907]] | mask 61676 px (4% of photo) | verify | 395 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but small and far from motorcycle, missing water around subjects. | accept (coverage partial) | 9473 |
| verify | two wheeler 1 wheel | muse-spark-open | crop [997,504,1407,914] px with 2 outlines A and B | chose B, shape 0.25 (fit 0.88, arc 66 deg, ratio 0.62, spill 0.02, closed ring), edge ratio 1.18, box agreement 0.62, is_target=true, outline partial, view side, edge visible 0.55, waterline visible; B follows the rear tyre arc above water while A is only the mudguard, but B's top-left edge cuts slightly inside the tyre and includes swingarm area. | give up: outline only partly right | 22613 |
| segment | people | sam-2.1-hiera-small | 2 person boxes with head and body points (segmented during the checks) | 2 masks | measure each against the stature prior | 1762 |
| segment | water below people | sam-2.1-hiera-small | 2 bands just below people (segmented during the checks) | 2 water masks | check that each outline ends at water | 4402 |
| geometry | person 1 | geometry | person mask | the shoulder line could not be found | declined | 1 |
| geometry | person 2 | geometry | person and water masks | depth 6.9 cm, sigma 22.8 cm, 96% of stature above water; scene model says the water is at the shin (25 cm) | candidate | 1 |
| estimate | photo | geometry | no measurable wheel; a measured person | depth 6.9 cm, sigma 22.8 cm | use the person | 0 |

### p_21960706: A car navigates a flooded street in Kolkata (Pexels, chosen for the wheel method)

Author Dibakar Roy (Pexels, dibakar-roy-2432543), licence Pexels License (https://www.pexels.com/license/), source https://www.pexels.com/photo/a-car-driving-on-a-flooded-street-21960706/.

Result: **model_estimate**, depth 35.0 cm, sigma 20.0 cm, confidence 0.22.
Scene model's own estimate: 35.0 cm (sigma 20.0 cm), basis: Water reaches mid-wheel and lower doors of stranded car across flooded street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car 1 wheel: the scene model did not confirm the mask (tyre seen head-on, its outline cannot be measured); no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water reaches mid-wheel and lower doors of stranded car across flooded street.

Wall time 67.6 s (timings ms: scene 1, agent 67432, geometry 0); model cost USD 0.027753.

Overlay: `eval/out/wheel-final/p_21960706_overlay.png`. Verifier images: `eval/out/wheel-final/p_21960706_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: car_wheel, car_wheel; own estimate 35 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 1 |
| segment | water | sam-2.1-hiera-small | box [8,304,500,532] px + points [[237, 440]] | mask 128187 px (8% of photo) | verify | 949 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [375,523,1405,1020] px | 87 proposals from 24 grid points, 2 scene model wheel boxes and 1 dark blob; best: wheel 0.56 = shape 0.60 (fit 0.97, arc 114 deg, ratio 0.55, spill 0.00, closed ring) x dark 0.85 x size 1.00; wheel 0.48 = shape 0.58 (fit 0.76, arc 180 deg, ratio 0.72, spill 0.00, closed ring) x dark 0.65 x size 1.00 | verify the best two | 6465 |
| verify | car 1 wheel | muse-spark-open | crop [841,587,1406,1067] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.35, waterline visible; Both magenta outlines trace water/splash beside the tyre, not the black tyre rubber arc above the water. | refine with box [1059,762,1126,846] px + points [1070,820], [1087,786], [1109,801], [1115,827] - points [1092,810], [1092,753], [1092,868] | 10058 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [1059,762,1126,846] px + points [1070,820], [1087,786], [1109,801], [1115,827] - points [1092,810], [1092,753], [1092,868] | 6 proposals, 5 distinct; best shapes 0.48, 0.25 | verify the best two | 1515 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta marks real floodwater but only a small patch behind the car and misses the water surrounding the vehicle. | refine with box [992,555,1568,939] px + point [1200, 726] | 18091 |
| segment | water | sam-2.1-hiera-small | box [992,555,1568,939] px + points [[1200, 726]] | mask 180020 px (11% of photo) | verify | 139 |
| verify | car 1 wheel | muse-spark-open | crop [964,677,1220,933] px with 2 outlines A and B | chose B, shape 0.25 (fit 1.00, arc 45 deg, ratio 0.63, spill 0.00, closed ring), edge ratio 0.78, not a tyre edge: the outline is an oval tilted 26 degrees: a cut or partial outline, not an upright wheel, box agreement 0.26, is_target=false, outline partial, view side, edge visible 0.45, waterline visible; B stays on the tyre rubber while A floods into water, but B captures only a narrow sliver of the sidewall not the full outer edge. | refine with box [1060,731,1127,834] px + points [1065,810], [1074,764], [1107,764], [1119,800] - points [1093,791], [1051,774], [1093,846] | 8150 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [1060,731,1127,834] px + points [1065,810], [1074,764], [1107,764], [1119,800] - points [1093,791], [1051,774], [1093,846] | 6 proposals, 6 distinct; best shapes 0.34, 0.14 | verify the best two | 1414 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta is water but only a small patch beside the car, missing most floodwater around and in front of it. | refine with box [24,747,928,1051] px + point [400, 907] | 9517 |
| segment | water | sam-2.1-hiera-small | box [24,747,928,1051] px + points [[400, 907]] | mask 232814 px (14% of photo) | verify | 128 |
| verify | car 1 wheel | muse-spark-open | crop [948,632,1239,924] px with 2 outlines A and B | chose B, shape 0.14 (fit 0.91, arc 93 deg, ratio 0.41, spill 0.13, closed ring), edge ratio 0.74, not a tyre edge: the outline is an oval tilted 27 degrees: a cut or partial outline, not an upright wheel, box agreement 0.84, is_target=true, outline good, view head_on, edge visible 0.55, waterline visible; B follows the narrow upright tyre band above the water while A covers only a thin sliver of the sidewall. | give up: tyre seen head-on, its outline cannot be measured | 15494 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on open floodwater directly behind and around the submerged car. | accept | 38482 |
| estimate | photo | muse-spark-open | no measurable wheel | 35 cm, sigma 20 cm | report model estimate | 0 |

### p_26202090: Vehicles submerged in a flooded street in Kolkata, India during a heavy monsoon rainfall (Pexels, chosen for the wheel method)

Author Dibakar Roy (Pexels, dibakar-roy-2432543), licence Pexels License (https://www.pexels.com/license/), source https://www.pexels.com/photo/cars-on-street-in-water-during-flood-26202090/.

Result: **model_estimate**, depth 38.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 38.0 cm (sigma 20.0 cm), basis: Water covers half of car tyres and reaches shin on standing person.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car 1 wheel: the scene model did not confirm the mask (outline and the tyre's box disagree); car 2 wheel: the scene model did not confirm the mask (outline and the tyre's box disagree); car wheel 3: the scene model did not confirm the mask (A is closer to tyre than B's tiny bumper sliver but A encloses mudguard/bodywork above tyre so it is not tyre-only and would give wrong water depth.); person 1: the shoulder line could not be found; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers half of car tyres and reaches shin on standing person.

Wall time 66.3 s (timings ms: scene 2, agent 66191, geometry 0); model cost USD 0.052394.

Overlay: `eval/out/wheel-final/p_26202090_overlay.png`. Verifier images: `eval/out/wheel-final/p_26202090_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1064 px (cached reading) | water visible; references: car_wheel, car_wheel, car_wheel, person; own estimate 38 cm | search 2 vehicle boxes for wheels, check 3 wheels and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [22,749,302,1051] px + points [[134, 896]] | mask 73000 px (4% of photo) | verify | 2589 |
| segment | car wheel 3 | sam-2.1-hiera-small | box [336,689,454,793] px + points [352,729], [392,702], [435,729] - points [403,768], [336,755], [453,755] | 6 proposals, 5 distinct; best shapes 0.38, 0.30 | verify the best two | 4684 |
| segment | car 2 wheel | sam-2.1-hiera-small | lower part of the vehicle box [260,504,1014,719] px | 78 proposals from 24 grid points and 2 dark blobs; best: wheel 0.90 = shape 0.92 (fit 1.00, arc 165 deg, ratio 0.78, spill 0.00) x dark 0.97 x size 1.00; wheel 0.18 = shape 0.28 (fit 0.72, arc 96 deg, ratio 0.76, spill 0.00) x dark 0.28 x size 1.00 | verify the best two | 12041 |
| verify | car wheel 3 | muse-spark-open | crop [191,544,597,949] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Both magenta outlines are in open flood water with no tyre inside the yellow box; the visible car tyres are outside the box and not outlined. | refine with box [336,689,454,793] px - points [394,746], [467,686], [536,726] | 8169 |
| segment | car wheel 3 | sam-2.1-hiera-small | box [336,689,454,793] px - points [394,746], [467,686], [536,726] | 3 proposals, 3 distinct; best shapes 0.48, 0.28 | verify the best two | 355 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage partial; Magenta patch is water but is a tiny corner crop that misses the main floodwater around the stranded cars. | refine with box [512,798,1248,1059] px + point [880, 979] | 12135 |
| segment | water | sam-2.1-hiera-small | box [512,798,1248,1059] px + points [[880, 979]] | mask 158837 px (9% of photo) | verify | 1144 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [453,628,1538,973] px | 86 proposals from 24 grid points, 2 scene model wheel boxes and 1 dark blob; best: wheel 0.73 = shape 0.76 (fit 0.89, arc 174 deg, ratio 0.72, spill 0.00) x dark 0.93 x size 1.00; wheel 0.68 = shape 0.73 (fit 0.92, arc 156 deg, ratio 0.69, spill 0.00, closed ring) x dark 0.86 x size 1.00 | verify the best two | 18485 |
| verify | car 2 wheel | muse-spark-open | crop [301,418,821,938] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.50, waterline visible; Both outlines follow the mudguard and bodywork, not the round outer edge of the black tyre above the water. | refine with box [491,722,634,790] px + points [504,761], [535,727], [592,727], [621,761] - points [561,774], [561,701], [483,722] | 9153 |
| segment | car 2 wheel | sam-2.1-hiera-small | box [491,722,634,790] px + points [504,761], [535,727], [592,727], [621,761] - points [561,774], [561,701], [483,722] | 6 proposals, 6 distinct; best shapes 0.58, 0.26 | verify the best two | 2025 |
| verify | car 1 wheel | muse-spark-open | crop [952,577,1505,1064] px with 2 outlines A and B | chose B, shape 0.73 (fit 0.92, arc 156 deg, ratio 0.69, spill 0.00, closed ring), edge ratio 0.26, box agreement 0.88, is_target=true, outline good, view side, edge visible 0.60, waterline visible; B follows the tyre outer edge above water with straight waterline while A incorrectly includes submerged part below water | refine with box [1122,802,1335,905] px + points [1134,857], [1227,810], [1295,818], [1324,857] - points [1233,867], [1231,784], [1372,823] | 8106 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the brown floodwater immediately surrounding the stranded white car. | accept | 12397 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [1122,802,1335,905] px + points [1134,857], [1227,810], [1295,818], [1324,857] - points [1233,867], [1231,784], [1372,823] | 6 proposals, 5 distinct; best shapes 0.47, 0.42 | verify the best two | 1840 |
| verify | car 2 wheel | muse-spark-open | crop [349,541,771,962] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.50, waterline visible; Neither outline follows the tyre outer edge, both cover water or a tiny splash artifact instead of the tyre. | refine with box [482,699,613,758] px + points [490,737], [528,707], [579,707], [604,737] - points [547,745], [469,690], [545,770] | 8251 |
| segment | car 2 wheel | sam-2.1-hiera-small | box [482,699,613,758] px + points [490,737], [528,707], [579,707], [604,737] - points [547,745], [469,690], [545,770] | 6 proposals, 5 distinct; best shapes 0.68, 0.53 | verify the best two | 1707 |
| verify | car wheel 3 | muse-spark-open | crop [224,575,556,907] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; Both magenta outlines are on water/debris in centre, not on the white car's front tyre at right, so neither follows the tyre. | refine with box [224,680,500,759] px + points [511,736], [521,693], [544,691], [553,731] - points [533,723], [505,670], [490,756] | 32506 |
| verify | car 1 wheel | muse-spark-open | crop [898,518,1561,1064] px with 2 outlines A and B | chose B, shape 0.42 (fit 0.82, arc 114 deg, ratio 0.68, spill 0.00, closed ring), edge ratio 8.30, box agreement 0.55, is_target=true, outline good, view side, edge visible 0.65, waterline visible; B follows the tyre's outer edge above the water with rim inside, while A is a small sliver on the bodywork not the tyre. | refine with box [1120,783,1339,870] px + points [1140,837], [1220,794], [1306,799], [1322,837] - points [1230,848], [1230,766], [1375,802] | 18600 |
| segment | car wheel 3 | sam-2.1-hiera-small | box [224,680,500,759] px + points [511,736], [521,693], [544,691], [553,731] - points [533,723], [505,670], [490,756] | 6 proposals, 5 distinct; best shapes 0.76, 0.60 | verify the best two | 2638 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [1120,783,1339,870] px + points [1140,837], [1220,794], [1306,799], [1322,837] - points [1230,848], [1230,766], [1375,802] | 6 proposals, 5 distinct; best shapes 1.00, 0.97 | verify the best two | 2012 |
| verify | car wheel 3 | muse-spark-open | crop [0,177,964,1064] px with 2 outlines A and B | chose A, shape 0.76 (fit 0.87, arc 198 deg, ratio 0.77, spill 0.00), edge ratio 14.30, box agreement 0.38, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; A is closer to tyre than B's tiny bumper sliver but A encloses mudguard/bodywork above tyre so it is not tyre-only and would give wrong water depth. | give up | 7952 |
| verify | car 1 wheel | muse-spark-open | crop [903,507,1556,1064] px with 2 outlines A and B | chose B, shape 0.97 (fit 1.00, arc 174 deg, ratio 0.63, spill 0.00, closed ring), edge ratio 13.36, box agreement 0.40, is_target=true, outline good, view side, edge visible 0.55, waterline visible; B follows the tyre's outer rubber closely above the water while A dips into water and includes bodywork, so B gives correct water depth. | give up: the outline covers a different area than the tyre (0.40) | 12302 |
| verify | car 2 wheel | muse-spark-open | crop [367,536,741,911] px with 2 outlines A and B | chose B, shape 0.53 (fit 0.86, arc 135 deg, ratio 0.55, spill 0.01, closed ring), edge ratio 0.78, box agreement 0.67, is_target=true, outline good, view side, edge visible 0.45, waterline visible; B follows the tyre outer edge above water with rim inside and straight waterline, while A covers only a small patch of sidewall. | give up: the outline covers a different area than the tyre (0.67) | 32801 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points (segmented during the checks) | 1 masks | measure each against the stature prior | 1647 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 10236 |
| geometry | person 1 | geometry | person mask | the shoulder line could not be found | declined | 0 |
| estimate | photo | muse-spark-open | no measurable wheel | 38 cm, sigma 20 cm | report model estimate | 0 |

### p_20989108: Flooded street in Kolkata with a car being pushed by a man through the waterlogged area (Pexels, chosen for the wheel method)

Author Dibakar Roy (Pexels, dibakar-roy-2432543), licence Pexels License (https://www.pexels.com/license/), source https://www.pexels.com/photo/man-pushing-car-on-street-in-water-20989108/.

Result: **model_estimate**, depth 50.0 cm, sigma 20.0 cm, confidence 0.23.
Scene model's own estimate: 50.0 cm (sigma 20.0 cm), basis: Water covers lower half of car wheels and reaches waist of adult pusher.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car 1 wheel: the scene model did not confirm the mask (Tyre is fully submerged below waterline and not visible above water; A follows fender/bodywork and B follows headlight, neither follows tyre rubber.); person 1: the shoulder line could not be found; no wheel could be measured; depth is the scene model's visual estimate, not a measurement; estimate based on: Water covers lower half of car wheels and reaches waist of adult pusher.

Wall time 57.1 s (timings ms: scene 2, agent 56980, geometry 0); model cost USD 0.019173.

Overlay: `eval/out/wheel-final/p_20989108_overlay.png`. Verifier images: `eval/out/wheel-final/p_20989108_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: car_wheel, car_wheel, person; own estimate 50 cm | search 1 vehicle box for wheels, check 2 wheels and segment the water | 2 |
| segment | water | sam-2.1-hiera-small | box [992,512,1577,769] px + points [[1248, 619]] | mask 129342 px (8% of photo) | verify | 2210 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [210,570,1295,959] px | 93 proposals from 24 grid points, 2 scene model wheel boxes and 3 dark blobs; best: wheel 0.49 = shape 0.63 (fit 0.87, arc 150 deg, ratio 0.45, spill 0.00, closed ring) x dark 0.56 x size 1.00; wheel 0.43 = shape 0.52 (fit 0.84, arc 135 deg, ratio 0.47, spill 0.00, closed ring) x dark 0.68 x size 1.00 | verify the best two | 7563 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies on the flooded street water surface adjacent to the submerged car and covers open water around it. | accept | 10349 |
| verify | car 1 wheel | muse-spark-open | crop [0,0,1600,1067] px with 2 outlines A and B | chose B, shape 0.52 (fit 0.84, arc 135 deg, ratio 0.47, spill 0.00, closed ring), edge ratio 1.66, box agreement 0.34, is_target=false, outline partial, view oblique, edge visible 0.45, waterline visible; B is the wheel but its outline bulges to include the mudguard and bodywork above the tyre instead of the tyre edge alone. | refine with box [573,667,771,838] px + points [589,779], [648,694], [728,710], [752,779] - points [664,763], [664,667], [616,635] | 9463 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [573,667,771,838] px + points [589,779], [648,694], [728,710], [752,779] - points [664,763], [664,667], [616,635] | 6 proposals, 5 distinct; best shapes 0.32, 0.20 | verify the best two | 1459 |
| verify | car 1 wheel | muse-spark-open | crop [351,446,981,1067] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; No tyre rubber is visible above water, both outlines cover fender/headlight not tyre so neither follows tyre edge. | refine with box [573,667,771,838] px - points [578,756], [716,788], [616,850] | 27071 |
| segment | car 1 wheel | sam-2.1-hiera-small | box [573,667,771,838] px - points [578,756], [716,788], [616,850] | 3 proposals, 3 distinct; best shapes 0.50, 0.35 | verify the best two | 956 |
| verify | car 1 wheel | muse-spark-open | crop [300,393,1051,1067] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.00, waterline not visible; Tyre is fully submerged below waterline and not visible above water; A follows fender/bodywork and B follows headlight, neither follows tyre rubber. | give up | 10282 |
| segment | people | sam-2.1-hiera-small | 1 person box with head and body points (segmented during the checks) | 1 masks | measure each against the stature prior | 1355 |
| segment | water below people | sam-2.1-hiera-small | 1 band just below people (segmented during the checks) | 1 water mask | check that each outline ends at water | 3551 |
| geometry | person 1 | geometry | person mask | the shoulder line could not be found | declined | 2 |
| estimate | photo | muse-spark-open | no measurable wheel | 50 cm, sigma 20 cm | report model estimate | 0 |

### p_21960691: Black and white image of a flooded street in Kolkata showing cars and a decorated bus (Pexels, chosen for the wheel method)

Author Dibakar Roy (Pexels, dibakar-roy-2432543), licence Pexels License (https://www.pexels.com/license/), source https://www.pexels.com/photo/vehicles-driving-on-a-flooded-street-21960691/.

Result: **wheel_geometry**, depth 25.4 cm, sigma 9.0 cm, confidence 0.09.
Scene model's own estimate: 27.0 cm (sigma 20.0 cm), basis: Water covers lower third to half of car tyres across flooded street.
Captured at (EXIF): not recorded.

Reasons: standing water visible; car wheel found; wheel size from catalogue: Maruti Suzuki Swift; scene model waterline agrees within 7.3 cm.

Wall time 46.0 s (timings ms: scene 3, agent 45386, geometry 546); model cost USD 0.025807.

Overlay: `eval/out/wheel-final/p_21960691_overlay.png`. Verifier images: `eval/out/wheel-final/p_21960691_agent/`.

| Step | Target | Model | Input | Output | Decision | ms |
|---|---|---|---|---|---|---|
| scene | photo | muse-spark-open | photo 1600x1067 px (cached reading) | water visible; references: car_wheel, car_wheel, car_wheel; own estimate 27 cm | search 1 vehicle box for wheels, check 3 wheels and segment the water | 3 |
| segment | water | sam-2.1-hiera-small | box [512,800,1153,1052] px + points [[819, 920]] | mask 116283 px (7% of photo) | verify | 1755 |
| segment | car wheel 2 | sam-2.1-hiera-small | box [1011,659,1044,697] px + points [1016,667], [1027,656], [1040,667] - point [1027,678] | 6 proposals, 5 distinct; best shapes 0.66, 0.49 | verify the best two | 3126 |
| verify | water | muse-spark-open | photo with the proposed region tinted | is_water=true, coverage good; Magenta region lies entirely on the flooded roadway water surface in the foreground around the vehicles. | accept | 7009 |
| segment | car 1 wheel | sam-2.1-hiera-small | lower part of the vehicle box [0,556,934,892] px | 90 proposals from 24 grid points, 2 scene model wheel boxes and 2 dark blobs; best: wheel 0.47 = shape 0.51 (fit 0.81, arc 138 deg, ratio 0.59, spill 0.00) x dark 0.83 x size 1.00; wheel 0.40 = shape 0.47 (fit 0.75, arc 159 deg, ratio 0.84, spill 0.01) x dark 0.70 x size 1.00 | verify the best two | 10155 |
| verify | car 1 wheel | muse-spark-open | crop [650,590,906,846] px with 1 outline | chose A, shape 0.51 (fit 0.81, arc 138 deg, ratio 0.59, spill 0.00), edge ratio 1.27, box agreement 0.72, is_target=true, outline good, view side, edge visible 0.65, waterline visible; Magenta outline follows the round outer edge of the tyre above the water with rim inside and straight lower edge at waterline, no inclusion of bodywork. | accept | 6308 |
| verify | car wheel 2 | muse-spark-open | crop [890,532,1146,788] px with 2 outlines A and B | chose A, shape 0.66 (fit 0.83, arc 186 deg, ratio 0.67, spill 0.01), edge ratio 1.19, box agreement 0.22, is_target=false, outline wrong, view side, edge visible 0.55, waterline visible; B outlines the door and bodywork while A is closer to the rear tyre but still strays onto sill and water and does not follow the round tyre edge. | refine with box [1008,671,1048,695] px + points [1012,686], [1027,675], [1044,686] - points [1028,687], [1028,665], [1028,701] | 15919 |
| segment | car wheel 2 | sam-2.1-hiera-small | box [1008,671,1048,695] px + points [1012,686], [1027,675], [1044,686] - points [1028,687], [1028,665], [1028,701] | 6 proposals, 5 distinct; best shapes 0.32, 0.00 | verify the best two | 1326 |
| verify | car wheel 2 | muse-spark-open | crop [900,552,1156,808] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.35, waterline visible; Neither outline follows the round outer edge of the rear tyre above water; A encloses sill/bodywork and water, B is only a tiny sliver at the waterline. | refine with box [1022,688,1063,712] px + points [1024,706], [1036,693], [1055,702] - points [1041,702], [1040,684], [1022,692] | 13256 |
| segment | car wheel 2 | sam-2.1-hiera-small | box [1022,688,1063,712] px + points [1024,706], [1036,693], [1055,702] - points [1041,702], [1040,684], [1022,692] | 6 proposals, 5 distinct; best shapes 0.62, 0.00 | verify the best two | 1257 |
| verify | car wheel 2 | muse-spark-open | crop [916,574,1172,830] px with 2 outlines A and B | chose NONE, is_target=false, outline wrong, view side, edge visible 0.60, waterline visible; Neither magenta outline follows the car tyre; both are in the flooded gap between vehicles, not on the car wheel at left | give up | 9178 |
| segment | water at wheels | sam-2.1-hiera-small | 1 bands just below wheels where the waterline was seen | 3655 px of water added next to the tyres | join to the verified water | 1214 |
| geometry | car wheel 1 | geometry | wheel and water masks | depth 25.4 cm, sigma 5.3 cm, 171 degrees of rim visible | candidate | 545 |
| estimate | photo | geometry | measurement 25.4 cm and model estimate 26 cm | difference 1 cm, two combined sigma 44 cm | use the measurement | 0 |

<!-- wheel-eval:end -->
