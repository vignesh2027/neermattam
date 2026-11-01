# Real-photo evaluation

**There is no ground truth for these photos.** Nobody measured the water in them, so nothing here is an accuracy figure. What this shows is qualitative behaviour on real street photos: what the models located, what the verify-and-refine agent accepted or rejected and why, which method answered, and the real latency and cost. Accuracy figures for the geometry come from the synthetic evaluation in `docs/GEOMETRY.md`; field accuracy needs photos with measured depths.

Rerun from `perception/` with `uv run --extra local python ../eval/real_photos.py` (live: Muse Spark Open on Baseten, SAM 2.1 hiera-small locally; add `--modal` for hiera-large on the Modal CPU service). The SAM size comparison is `../eval/sam_ab.py` (table in `RESULTS_SAM_AB.md`) and the latency breakdown is `../eval/latency.py`. The photos are downloaded from Wikimedia Commons into `eval/photos/` and are not in git; their source, author and licence are in `eval/commons_photos.json` and repeated per photo below. Overlays, response JSON and every image the verifier was shown go to `eval/out/` (also not in git). There are two sets. The Unsplash close-ups (`eval/unsplash_photos.json`, Unsplash License, photographers credited there) are the real use case: a resident photographing a vehicle a few metres away. The Commons set (`eval/commons_photos.json`) is the four wide street photos named in the first brief (c1 to c4) plus two with motorcycles standing in water side-on (x1, x2).

**The current result is the generated section at the end of this file** (run of 2026-10-30, Muse Spark Open on Baseten and SAM 2.1 hiera-small locally). Of the eleven photos, none was measured from a wheel; two were measured from a person (c1 45.6 cm, sigma 13.0 cm, confidence 0.22; c3 102.9 cm, sigma 10.9 cm, confidence 0.28); the other nine got the scene model's labelled estimate (confidence 0.21 to 0.23). Wall time was 7.2 to 50.2 s per photo. A wheel-focused set of 22 more photos was added on 1 November; no wheel measurement on it met the bar for a demonstration, and the section "Photos chosen for the wheel method" says why. The hand-written sections below describe other runs, each with its date or output folder, and their numbers differ from the generated section because the scene reading is fresh on each run and the verifier is not deterministic.

## Photos chosen for the wheel method (1 November)

**Result: no wheel measurement on these photos meets the bar of an outline on the tyre's outer edge, a plausible depth, agreement with the scene model's estimate and three runs within sigma.** One photo was measured twice from a wheel (u_w8MXURXvqjQ, 15.2 and 12.2 cm), but its outline takes in the mudguard, so I do not count it. Every other wheel either was declined, or was measured and then set aside by the shallow-side guard. This section says what was collected, what happened to each photo and why. Nobody measured the water in any of these photos, so nothing here is accuracy. "By eye" means my own reading of the photo, which is not a measurement either.

**What was collected.** 22 photos of vehicles standing in shallow to moderate water with a tyre seen from the side, listed in `eval/wheel_photos.json` with author, licence, source page and what I saw when I chose each one. They come from Wikimedia Commons (8: four more of McKay Savage's 2008 Chennai monsoon series and one of his Kanchipuram photos, CC BY 2.0; a Kerala police Tata Sumo and a Borivali state bus, CC BY-SA 2.0; mopeds in Quang Ngai, CC BY-SA 3.0; a hatchback in Iskenderun, CC BY 3.0), Unsplash (7, Unsplash License: six by Aldward Castillo, one by Dibakar Roy) and Pexels (7, Pexels License, all by Dibakar Roy in Kolkata). Run them with `uv run --extra local python ../eval/real_photos.py --sets wheel`; the photos are downloaded into `eval/photos/` and stay out of git. That run writes its own generated block (marker `wheel-eval`), which is not in this file yet because no complete run finished (below).

**Runs.** All with SAM 2.1 hiera-small on this laptop and Muse Spark Open on Baseten at scene effort `low`, and with the scene reading cached after the first run so that later runs differ only in the checks:

| Output folder | Code | Settings | Photos finished |
|---|---|---|---|
| `eval/out/wheel-base` | main at 6b62807 | 55 s budget, as the service | 20 |
| `eval/out/wheel-nob` | main at 6b62807 | no budget (diagnosis) | 17 |
| `eval/out/wheel-v2`, `wheel-v3` | this branch, without the water change | no budget | 10 and 4 |
| `eval/out/wheel-v4` | this branch, without the water change | no budget | 18, then stopped |

In the 55 s run no photo got a wheel check through: the checks were stopped by the budget on all 20. The other runs were for finding out why.

**Why the runs could not finish: the uplink of this laptop.** On the evening of 1 November a text-only call to the same Baseten model answered in 1.6 and 1.9 s, while an image check (one or two 512 px JPEGs, 60 to 100 KB) took 10 to 45 s. Uploading 200 KB to an unrelated host ran at 2.3 to 7 KB/s, and at 3 to 29 KB/s over the following half hour; at one point another process on this machine was sending about 45 KB/s. A check is cut after 20 s and asked once more, so many checks failed with "gave no answer within 20 s", up to 5 per photo. The damage was worst on the water: when the water check failed, the photo had no verified water, and four wheels the agent had already accepted could not be measured (w_kanchipuram, p_23939154, p_21960691, u_lIkOxcck448), plus u_w8MXURXvqjQ in its third run. The deployed service calls Baseten from a data centre, so this is a limit of the evaluation machine, not a measurement of the service; it does mean the repeat runs this section needed could not be done here. After the first run the scene readings came from the cache, so the later runs' costs and times are those of the checks alone.

**What the agent did with the wheels it could check.** Across the four diagnosis runs, the agent accepted 13 wheel outlines on 8 photos. I looked at every verifier image and every overlay:

| Photo | Scene model's estimate | What was accepted | What happened |
|---|---|---|---|
| u_w8MXURXvqjQ (motorcycle, front wheel) | 23 cm | the tyre's outer edge on the left and bottom, the chrome mudguard at the top and right | measured 15.2 cm (sigma 3.7) and 12.2 cm (sigma 6.8); the scene model's waterline differed by 1.7 and 6.3 cm. The front wheel is in its own splash. A full-reasoning check of the same image called the outline wrong ("bulges at the top to enclose the silver mudguard"). Not counted. |
| u_ic8V3A59gpg (close-up of a car's front wheel) | 22 cm | a polygon inside the tyre: rim and the inner half of the rubber (a black tyre on a black car, so no edge to follow) | measured 2.7 cm (sigma 1.1) twice; set aside both times by the shallow-side guard. By eye the water is a little below the hub, a quarter to a third of the wheel. |
| w_kerala4x4 (motorcycle beside the 4x4) | 35 cm | the front wheel, its bottom on the bow wave | 1.6 cm (sigma 4.4), set aside by the guard |
| w_chennai01 (lorry's front tyre, called a bus wheel) | 15 cm | the tyre's outer edge, a good outline | measured 3.6 cm (sigma 16.2) against the 106 cm bus tyre, and published (main's code). A light truck's tyre is about 80 cm. Fixed below. |
| w_chennai01 (lorry's rear tyre) | 15 cm | the tyre plus its mudflap | 12.1 cm (sigma 12.0), not chosen |
| w_kerala4x4 (the 4x4's rear wheel) | 35 cm | the tyre above a splash | declined by the geometry: 90 degrees of rim visible |
| p_23939154 (Pulsar walked through water) | 27 cm | the tyre, its mudguard and a patch of water | water check timed out, not measured. The outline was wrong. |
| w_kanchipuram, p_21960691, u_lIkOxcck448 | 12, 27, 28 cm | outlines cut across the wheel by the fork (Kanchipuram) or on the rim (Swift) | water check timed out, not measured |

So of the outlines Muse Spark Open accepted at effort `low`, I would trust two (the lorry's front tyre and Kanchipuram's, the second with a fork cutting its corner). The numeric checks (shape, box agreement, darkness either side of the edge, tilt, size, the verifier's own points on the rubber) stopped many more, but not these. The shallow-side guard stopped every reading that was clearly too shallow except the lorry's, whose sigma was 16 cm.

**Why outlines miss the tyre's outer edge.** From the verifier images and from every SAM proposal for the same prompts, rendered offline (`dbg` renders, not kept):

* *Two-wheelers:* the mudguard hugs the top of the tyre, and SAM puts them in one mask whatever the prompt; a ring of points on the rubber with negative points just outside the verifier's tyre box (tried offline on u_w8MXURXvqjQ, w_chennai03, p_23939154) moved the outline onto the tyre on the left and top but kept the fork and mudguard on the right. The fork and brake disc split the visible tyre into two pieces, so the convex hull of the rubber is not the wheel either.
* *Cars:* the wheel arch above the tyre is a dark, round, tyre-sized void. On p_26202090's front wheel SAM's best-ranked proposal (shape 0.92, darkness 0.97) is the arch, not the tyre; the verifier rejected it. On dark cars the tyre has no edge against the body (u_ic8V3A59gpg) and the outline settles on the rim.
* *The verifier at reasoning effort `low`* calls such outlines good. At full reasoning it rejected two of three that I had judged wrong (u_ic8V3A59gpg, u_w8MXURXvqjQ) but accepted p_23939154's, and each such check took 25 to 45 s and USD 0.018 to 0.033 on this uplink, too slow for the 55 s budget.
* *The scene model's own estimates run deep for shallow water:* on p_26202090 it says 38 cm for the street and 42 cm at the rear wheel, where by eye the water is a little below the hub of a 62 cm tyre (about 25 to 30 cm). A correct reading there of about 27 cm with a sigma of 4 cm would be set aside by the guard. That is the guard working as designed (a reading shallower than the estimate's evidence is not published), and it means the wheel path can only publish where the scene model is not overestimating.

**What I changed** (each with tests, `perception/tests/`):

1. *A truck's tyre is no longer measured as a bus wheel* (`pipeline.truck_wheels_dropped`). The scene model has no truck wheel to choose; a bus or auto-rickshaw wheel inside a truck's box is dropped with the existing reason "no wheel size is known for a truck", unless it also lies in a bus or auto box. This removed the only wrong reading that was published (w_chennai01, 3.6 cm).
2. *The scene model's own wheels are prompted in the vehicle-box search* (`agent.find_vehicle_wheel`, `wheel_finder.grow_region`). Its wheel boxes and tyre points inside a vehicle used to be thrown away; on the close-up u_ic8V3A59gpg the tyre lay above the lower part of the car's box and the verifier was shown a fog light. With the hint the tyre was found and checked (and then correctly set aside, above).
3. *A proposal four times larger than its tyre box is not shown to the verifier* (`agent.spills_far_beyond`). On p_26202090 a box around the tyre gave the whole side of the car as panel B.
4. *A wheel found in a vehicle's box takes the position and the estimate of the scene model's wheel it overlaps* (`pipeline.matching_hint`), so front and rear tyres of a catalogue two-wheeler are not mixed, and the guard compares with the estimate at that wheel.
5. *Water confirmed by an earlier check is kept when a later water check fails* (`agent.refine_water`), as it already was when the budget ran out; this is the failure that cost the four accepted wheels above.
6. *Evaluation:* `real_photos.py` has the new set (`--sets wheel`), its own results block (`--marker wheel-eval`), and diagnosis flags `--budget 0` and `--verify-timeout`; Unsplash downloads fall back to `curl -4` when the HTTP client gets a 403.

None of the safety rules changed: the shallow-side guard, the brightness check across the edge, unknown is never safe, and labelling are as they were; changes 1 and 3 only remove things, and 2, 4 and 5 add prompts or keep evidence the model already confirmed.

**Cost and time.** Baseten spend for everything in this section, including the offline checks of single images, was about USD 1.05 (sum of `cost_usd` over the runs, USD 0.95, plus probes). Wall time per photo in the diagnosis runs was 44 to 113 s (median 68 s, 45 photo runs), almost all of it waiting on uploads.

**What it would take.** A run of `--sets wheel` from a machine with a normal uplink, with the code on this branch, to see how many wheels the agent accepts when every check gets an answer; three repeats of any photo that measures. On the evidence above I expect few: the outlines, not the geometry or the guard, are what fails. The two directions that look worth trying are a full-reasoning confirmation only for the one outline about to be measured (it caught two of three bad outlines here), and a tyre outline built from the rim, which SAM finds reliably, outwards to the dark rubber; a first try of the second on p_26202090 followed the wheel arch instead of the tyre, so it is not ready.

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
