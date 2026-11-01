# Geometry: water depth from one photo, with a wheel as the ruler

This note explains how `perception/` turns two masks (a wheel and the water) into a depth with an uncertainty, what can go wrong, and how well it works on synthetic scenes. Code: `perception/src/neermattam_perception/geometry.py`. Every number in the results section is produced by `uv run python -m neermattam_perception.synth_eval` and can be rerun.

## 1. The idea in one paragraph

A wheel is a circle of known diameter standing in a vertical plane. Floodwater hides its lower part. The waterline across the wheel is a horizontal chord of that circle, and the water depth at the wheel is the height of that chord above the bottom of the circle. So the depth is a fraction of the diameter: `depth = f x D`. The fraction `f` is a ratio of two lengths along the same line inside one object, which is why the camera distance and the focal length drop out. SAM 3.1 gives the masks, Muse Spark 1.3 says where to look, and the geometry below does the rest. On the open-weights path SAM 2.1 and Muse Spark Open play the same roles, and Muse Spark Open also checks every mask on a close-up before it is measured (`agent.py`).

## 2. The construction

### 2.1 What the camera does to a circle

For an object that is small compared with its distance from the camera, a pinhole camera acts on it like an affine map (a linear map plus a shift). Under an affine map:

* a circle becomes an ellipse;
* straight lines stay straight and parallel lines stay parallel;
* ratios of lengths along one line, or along parallel lines, are preserved;
* a pair of conjugate diameters stays conjugate. (Two diameters of an ellipse are conjugate when each one bisects every chord parallel to the other. In a circle, conjugate means perpendicular.)

### 2.2 The vertical diameter is the diameter conjugate to the waterline

In the wheel's own plane:

```
            T  (top of the wheel)
            |
        .---|---.
      /     |     \
     |      |      |
 ----W1-----M-----W2----   waterline: a horizontal chord
     |      |      |        (water below this line)
      \     |     /
        '---|---'
            B  (bottom, on the road)

   depth d = |BM|,  diameter D = |BT|,  f = d / D = |BM| / |BT|
```

The waterline `W1 W2` is horizontal and the diameter `T B` is vertical, so they are perpendicular, and `TB` bisects `W1 W2` at `M`. In the image the circle is an ellipse and `W1 W2` is still a straight chord. Perpendicular diameters of a circle map to conjugate diameters of the ellipse, so the image of `TB` is the diameter of the ellipse conjugate to the waterline's direction: the line through the ellipse centre that bisects every chord parallel to the waterline. Ratios along that line are preserved, so `f = |BM| / |BT|` can be read straight off the image.

This answers the question of which ellipse axis to measure along: neither, in general. The vertical diameter coincides with the minor axis only when the waterline happens to be parallel to the major axis (a level camera looking straight at a wheel turned away from it). With camera pitch, roll, or a waterline seen at a slant, the conjugate diameter is the right one, and it is not an axis of the ellipse.

### 2.3 Computing it

Let the fitted ellipse have centre `c`, semi-axes `a >= b` and angle `theta`. The affine map

```
u = diag(1/a, 1/b) R(-theta) (p - c)
```

sends the ellipse to the unit circle. Map the waterline's point and direction through it. In the unit circle, conjugate means perpendicular again, so take the unit normal `n` of the mapped waterline, oriented towards the water side, and the signed distance `s` of the mapped line from the centre along `n`. The bottom of the wheel is at `+n`, the top at `-n`, the waterline crosses at `s n`, and

```
f = (1 - s) / 2          depth = f x D
```

`s = 1` means the water just touches the bottom (`f = 0`); `s = -1` means the wheel is fully under (`f = 1`). Water above the hub needs no special case: the centre then lies on the water side of the line, `s` is negative and `f` is above one half. The overlay draws the vertical diameter from `s n` to `+n` mapped back into the image.

### 2.4 The exact perspective version

When the wheel is close or the camera is steep, the affine approximation leaves a small error, because a pinhole camera does not preserve ratios along a line. With known intrinsics `K` the error can be removed. The image ellipse `C` (a 3 x 3 conic matrix) back-projects to the cone `Q = K^T C K`. Its eigenvalues `l1 >= l2 > 0 > l3` give the circle's plane normal up to two solutions,

```
n = +/- sqrt((l1 - l2) / (l1 - l3)) e1 + sqrt((l2 - l3) / (l1 - l3)) e3
```

For each, rotate the camera virtually until that plane faces it (the homography `K R K^-1`). The ellipse becomes a circle and the construction of section 2.3 becomes exact. We keep the solution whose implied vertical, the in-plane direction perpendicular to the waterline and pointing away from the water, best matches "up" in the photo, since wheels stand upright and phones are held roughly level. `geometry.projective_fraction` implements this, and a unit test checks it is exact on a projected circle where the affine answer is off by more than 1 percent of the diameter.

The focal length comes from EXIF `FocalLengthIn35mmFilm` when present (35 mm equivalents match the 43.27 mm film diagonal, so `f_px = f35 x diagonal_px / 43.27`). On the synthetic set the correction did not beat the affine model once mask noise and fitting error were present (see the results), so the pipeline leaves it off by default (`PipelineConfig.perspective = "off"`). Note that the affine error has a sign: a camera held above the water sees the top of the wheel slightly closer, and so slightly larger, than the bottom, which shrinks the apparent submerged share. With exact ellipses and exact waterlines the affine depth read 0.6 cm low on average over 312 scenes of a development seed (31337). The bias term of section 4 absorbs it along with the fitting bias.

### 2.5 A real tyre is a short cylinder, not a disc

Seen at an angle, a tyre shows its tread, and its outline is the convex hull of two ellipses: the near sidewall and the far sidewall. Fitting one ellipse to that outline makes it too fat, which biased depths low in early tests. The outline model fits both sidewalls at once:

* Under an affine camera, a circle of radius `R` whose normal is tilted by `phi` from the line of sight images with semi-axes `R` and `R cos(phi)`, and its unit normal images along the minor axis with length `sin(phi)`. So the far sidewall is the near one shifted along the minor axis by `t x a x sqrt(1 - (b/a)^2)`, where `t` is the tyre width over the radius. One scalar describes the tread.
* Perspective makes the far sidewall slightly smaller; one extra scale, bounded to 0.9 to 1.11, covers it.
* Each boundary point is scored by its signed orthogonal distance to whichever sidewall ellipse is nearer.
* The tyre model is only accepted when it lowers the robust cost by at least 15 percent and splits the outline into long contiguous runs on each sidewall. Taking the nearer of two ellipses always absorbs some noise, so without the coherence test a thin wheel looks like a tyre.
* The waterline is cut in the near sidewall, so the two ends of the visible waterline lie on the near ellipse. They enter the fit as weighted anchor points and decide which sidewall is near.
* When SAM returns a rim mask, the rim's edge is fitted jointly. It sits on the near sidewall, concentric with it, with its own free scale (so flange height does not matter). It pins the centre and shape when little tyre is above water, and settles which sidewall is near.

## 3. The algorithm, step by step

1. **Outline.** Fill holes in the wheel mask and trace its outer contour. OpenCV traces pixel centres half a pixel inside the true edge, so each point is moved half a pixel outwards along its normal.
2. **Contact points.** Outline points within 2.5 px of the water mask.
3. **First waterline.** RANSAC line through the contact points. Contact points whose outward normal faces the water (within 60 degrees) are waterline candidates and stay out of the first fit. Contact points facing sideways or up are the true wheel edge with water seen behind it, and stay in.
4. **Occluders.** A wheel has no straight edges. Long straight runs on the dry outline, longer than any arc could mimic within tolerance, are bodywork, mudguards or the image border, and are dropped, unless they lie on the fitted outline (a narrow ellipse is nearly straight at the ends of its minor axis).
5. **First outline.** Robust ellipse fit (below), then the outline model.
6. **Waterline chord.** The contact points lying clearly inside the outline (normalised radius below 0.97). A RANSAC line through them is the waterline. Its water side is checked against the water mask.
7. **Final outline.** Refit on the outline points clearly on the dry side, with the waterline ends as anchors and the rim if present.
8. **Checks and depth.** Visible arc, plausibility, waterline scatter, then the fraction, the alternatives check and the Monte Carlo.

**Robust ellipse fitting.** Candidate ellipses come from the direct least-squares fit of Fitzgibbon, Pilu and Fisher (1999) in the numerically stable form of Halir and Flusser (1998), on centred, scaled points. Its constraint `4AC - B^2 = 1` guarantees an ellipse, which makes it safe inside RANSAC. Minimal samples of 5 points are scored with a truncated Sampson cost (MSAC), then refined by local optimisation on the inliers. To stop a short arc exploding into a huge ellipse, every candidate must pass physical limits before it is scored: axis ratio at least 0.3 (a wheel seen more than about 72 degrees off its axis is unusable), semi-major axis at most 0.8 times the arc's extent plus 2 px (a 100 degree arc spans a chord of at least 1.53 R), and a bounded centre. The final refinement minimises the true orthogonal distance (a vectorised Newton solve on the ellipse parameter), not the Sampson approximation, which is biased on partial arcs of eccentric ellipses.

**Typed failures.** `measure_wheel_depth` returns a `DepthFailure` with one of these reasons; the service puts the text in `reasons`.

| Reason | Meaning |
|---|---|
| `wheel_mask_empty` | SAM returned no pixels for the wheel (it may be fully under water) |
| `wheel_too_small` | under 600 px of wheel |
| `no_water_contact` | water does not meet the wheel, or only touches its outline |
| `arc_too_short` | less than 100 degrees of rim outline visible above water and occluders |
| `ellipse_fit_failed` | no plausible ellipse |
| `implausible_ellipse` | the wheel mask spills outside the fitted outline |
| `waterline_ambiguous` | no straight waterline, no water beyond it, or the waterline points are scattered (splash) |
| `waterline_below_wheel` | the waterline misses the fitted wheel |
| `fully_submerged` | the waterline is above the fitted wheel |
| `ambiguous_outline` | fits of nearly equal quality disagree by more than 15 percent of the diameter |

## 4. Uncertainty

`depth_sigma_cm` combines three parts in quadrature:

1. **Resampling the measurement.** Each of 100 draws (in the service) bootstraps the outline points with pixel-quantisation jitter and refits the outline; bootstraps the waterline points and refits the line; shifts the line by a level error equal to the observed ripple (ripples are spatially correlated along the chord, so they do not average out); and draws a diameter from `N(D, sigma_D)`. Two ways of refitting the draws are implemented (`GeometryConfig.uncertainty`):
   * `monte_carlo`, the reference: every draw's outline is refitted by the nonlinear solver, one call per draw. It took about 2.3 s per wheel at 100 draws (median, one process, M1), so a photo with several candidate wheels took many seconds.
   * `linear`, the service default: the same draws, in the same roles and from the same random streams, but all outline refits are solved together as one batch of iteratively reweighted Gauss-Newton steps (four steps, the solver's soft L1 weights, forward-difference Jacobians formed for all draws at once, a step kept only where it lowers the robust cost). It took 0.94 s per wheel (median; 90th percentile 1.5 s against 3.4 s), most of which is now the single full fit that both methods share, and the wheels of one photo are measured on parallel threads.
   * The batched refits come out a little narrower than full refits, mostly in the tail where a full refit jumps to another local fit: on 124 usable cases of the calibration seed 9945 the batched sigma was 0.94 times the Monte Carlo sigma at the median (10th to 90th percentile 0.72 to 1.09), and the root-mean-square Monte Carlo sigma was 1.14 times the batched one. The batched sigma is therefore multiplied by `geometry.LINEAR_SIGMA_SCALE = 1.14`, the larger of the two factors. These numbers come from `uv run python ../eval/sigma_methods.py` (`--cases 160 --workers 7` for the ratios, `--cases 40 --workers 1` for the times).
2. **Model ambiguity.** The fit is started from several tread hypotheses. All fits within 15 percent of the best cost are kept, and the spread of the depths they imply enters as a uniform range.
3. **Model error.** Resampling the same masks cannot reveal fit bias on short arcs, residual perspective or tyre shape. The errors are one-sided: on short arcs the fit sometimes locks onto the tread and far sidewall and returns a near sidewall that is too small and sits too high, so the depth reads low, sometimes by 20 to 40 cm, and almost never reads high by much. Two tables against visible arc, as shares of the diameter, are calibrated on synthetic scenes with a seed disjoint from the evaluation (`synth_eval --calibrate`):
   * `geometry.MODEL_BIAS_TABLE`: the median signed error per arc bin. It is subtracted from the fraction. It is clamped to be no more than zero, so the correction can only raise a depth.
   * `geometry.MODEL_SIGMA_TABLE`: after the bias is removed, the smallest model sigma that, combined with each case's Monte Carlo sigma, puts 68 percent of absolute errors within one sigma and leaves no more than 6.7 percent of true depths above `depth + 1.5 sigma`, the line at which the engine blocks a road. The second condition is the binding one; it is what the heavy low tail costs in sigma.

   Both are monotone in arc (more visible arc never hurts).

The `confidence` field is a 0 to 1 ranking score built from the scene model's image-quality score, the relative sigma and the agreement of the two waterlines. It is not a probability.

**Second waterline.** Muse Spark independently points to where the water meets the wheel's left and right edges (on the open-weights path, the verifier gives these points on its close-up crop of the accepted wheel). That line is measured on the same fitted wheel, and `waterline.agreement_cm` is the difference between the two depths. It flags splash, reflections and mask errors that the masks alone cannot see. Since we cannot tell which of the two lines is wrong, the difference is added to `depth_sigma_cm` in quadrature, so a disagreement widens the uncertainty as well as lowering the confidence.

**Corrections applied after the geometry** (`pipeline.py`), each carrying its own uncertainty:

* Tyre squash (section 5) is subtracted for the tyres with a listed loaded radius, and half of the correction is added to the sigma in quadrature (`references.SQUASH_REL_SIGMA`): the listed loaded radius holds at the rated load and pressure, while a vehicle in the street carries anything from its kerb weight upwards, and deflection grows roughly with load. That half is an assumption, not a measurement.
* When no wheel can be measured, the scene model's own visual estimate is returned as `method: model_estimate`, with a sigma of at least 20 cm or 30 percent of the estimate, whichever is larger (and at least half of any range the model gave). When both exist, the geometry is used and the estimate is reported beside it; if they differ by more than twice their combined sigma, the confidence is halved and the reasons say so.

## 5. Error budget

| Source | Effect | Handling |
|---|---|---|
| Mask edge noise | wobbles the outline by 1 to 2 px; biases short arcs | robust fit, orthogonal distance, Monte Carlo, model table |
| Little arc above water | the fit is poorly conditioned once less than about 150 degrees shows | declined below 100 degrees; sigma grows through the model table; the pipeline prefers the wheel with the smallest sigma, so a bigger wheel in view wins |
| Ripples and wakes | move the waterline locally by about their amplitude | robust line fit; level error in the Monte Carlo equals the observed scatter |
| Splash hiding the waterline | scattered chord points | declined when the scatter is large; the second waterline disagrees and the disagreement widens sigma |
| Water seen behind the wheel | touches the outline above the waterline | orientation rule in step 3; only points cutting through the outline form the chord |
| Bodywork over the wheel | straight occluding edge | straight-run removal; arc check |
| Tyre width at oblique views | outline is two sidewalls | two-sidewall model, anchors, rim |
| Perspective | ratios not exactly preserved at close range | exact correction available; measured on the synthetic set |
| Wheel off vertical | a wheel leaning by `e` makes the in-plane distance `d / cos(e)`: +1.5 percent at 10 degrees, +3.5 percent at 15 degrees (a scooter on its side stand) | not corrected; part of the stated limits |
| Tyre squash | a loaded tyre's axle sits below its unloaded radius, so the fitted circle reaches below the road by the deflection and the depth reads high by it | subtracted for the auto-rickshaw (4.00-8, 1.9 cm) and the bus (10.00 R20, 3.2 cm), whose loaded radii come from tyre listings, with half the correction added to sigma in quadrature for the unknown load; for two-wheeler and car tyres no verified figure was found, so no correction is made and those depths may read slightly high |
| Tread wear | a worn tread is smaller by a few millimetres of radius | inside the per-tyre sigma |
| Wheel size | catalogue tyre: sigma 1.0 cm (two-wheeler), 1.5 cm (car), 2.0 cm (auto, bus); variants mixed when the position or trim is unknown; generic priors are the mixture of all catalogue wheels of the kind | carried into the Monte Carlo |

## 6. Velocity

With a video, Muse Spark Open boxes up to eight floating objects and one fixed anchor (a sign, a pole base, a road marking on dry ground) on the key frame, and SAM 2.1's video predictor follows each box forward for up to 4 s at about 10 frames per second (`perception/src/neermattam_perception/flow.py`). With SAM 3.1 the scene reading's noun phrase is tracked instead. Before tracking, each box is checked on the key frame: it must hold at least 4 connected pixels more than 4 robust sigmas from the grey level around it. On real footage the scene model's boxes on small debris were often a few pixels off the object, on plain water, so each box is snapped to the nearest such object within one box size, or dropped; both are recorded as `validate` steps. A track lost on more than half the frames, or whose mask changes size more than three times on more than 30 percent of frames (it slid onto a wall, a wave or a neighbour), is set aside; single out-of-range frames are dropped. When the anchor was found on at least 90 percent of frames, its motion is subtracted from every track, which removes pans and shake. Each track's centroids become a path in centimetres using the reference's scale: for a wheel `D / (2a)` cm per pixel, from the semi-major axis, which is the wheel radius without foreshortening; for a person, the head-to-shoulder length is 0.182 of stature (section 9), with the stature, proportion and posture sigmas added. A Theil-Sen line of position against time gives each track's velocity, robust to a bad mask on a frame. Tracks that are too short, do not move measurably (speed within two sigma of zero), move against the common flow, or sit far from the median speed are dropped, each with its reason; tracks that cancel out with no common direction give no speed. Surface speed is the median of the rest, and its sigma is never smaller than the kept tracks' own fit sigma allows. Real-video results and a known-motion check are in `eval/RESULTS_VIDEO.md`.

Depth-averaged velocity is taken as 0.85 times the surface velocity: "A coefficient of 0.85 is commonly used to convert the velocity of a surface float to mean velocity in the vertical" (Rantz et al. 1982, USGS Water-Supply Paper 2175, vol. 1, p. 262, doi:10.3133/wsp2175; the same value appears in WMO 2010, Manual on Stream Gauging, WMO-No. 1044, vol. I, section 8.3). Hauet et al. (2018, E3S Web of Conferences 40, 06015, doi:10.1051/e3sconf/20184006015) found a mean of about 0.8 over 3611 LSPIV gaugings, with 5th to 95th percentiles of 0.7 and 0.9; we carry 0.06 as the coefficient's sigma. Limits: only motion in the image plane is seen; the scale is right only at the reference's distance; the anchor removes image shifts, not zoom or a camera walking along the street.

## 7. Synthetic evaluation

**How the scenes are made** (`synth.py`). Each scene is a 3D wheel seen through a pinhole camera, not a 2D ellipse, so perspective error is part of the result. The world has the road at height 0, the wheel upright at the origin, and the water as a plane at the chosen depth; the water mask is ray-cast per pixel. Parameters are drawn per case:

* wheel diameter from the catalogue (42.6 to 106.4 cm);
* depth 5 to 80 percent of the diameter;
* ellipse axis ratio 0.5 to 1.0 (the camera turned up to 60 degrees off the axle), pitch 3 to 30 degrees, roll up to 8 degrees;
* camera 1.5 to 6 m away with a main-camera to 2x lens, the wheel 100 to 450 px across in a 960 x 720 image;
* tyre width 0 to 30 percent of the diameter, rim at 60 percent;
* water visible behind the wheel in half the cases, bodywork hiding the top of the wheel in a fifth;
* ripples up to 2 px, and smooth mask-edge noise of 0.3 to 1.5 px applied to the signed distance from each edge, as a stand-in for SAM's errors.

Masks are rasterised by pixel coverage (4x supersampling), since filling polygons at native resolution dilates them by about half a pixel.

**What it does not cover.** Real SAM masks, real tyres (rounded shoulders, tread blocks, mud), reflections, motion blur, people or debris in front of the wheel, wheel lean and tyre squash. These numbers bound the geometry; they are not field accuracy. Field accuracy needs photos with measured depths, which belongs in `eval/`.

**Results.** Generated by the command shown in the first line; the true diameter is used, so diameter uncertainty adds `f x sigma_D` on top.

<!-- synth-eval:begin -->
Generated by `uv run python -m neermattam_perception.synth_eval --cases 500 --seed 2026 --draws 100` (100 resampling draws per case, propagated by batched Gauss-Newton refits; 329 s wall time).

**All cases, by depth method** (errors over usable cases only)

| Subset | Cases | Usable | MAE cm | Median cm | P90 cm | Bias cm | MAE / diameter |
|---|---|---|---|---|---|---|---|
| tyre + rim, affine, bias-corrected (pipeline default) | 500 | 78% | 3.34 | 1.25 | 7.68 | -2.05 | 5.3% |
| tyre + rim, perspective with EXIF focal, uncorrected | 500 | 78% | 4.00 | 1.48 | 9.84 | -3.19 | 6.4% |
| tyre + rim, perspective with assumed 26 mm, uncorrected | 500 | 78% | 4.01 | 1.60 | 9.04 | -3.20 | 6.4% |
| tyre outline only, affine, uncorrected | 392 | 98% | 7.21 | 4.81 | 15.70 | -7.19 | 11.5% |

**Breakdown for tyre + rim, affine, bias-corrected (pipeline default)**

| Subset | Cases | Usable | MAE cm | Median cm | P90 cm | Bias cm | MAE / diameter |
|---|---|---|---|---|---|---|---|
| depth 5 to 25% of diameter | 128 | 90% | 1.11 | 0.68 | 2.24 | +0.30 | 1.8% |
| depth 25 to 50% | 164 | 85% | 2.48 | 1.14 | 6.65 | -1.74 | 3.9% |
| depth 50 to 65% | 114 | 74% | 4.35 | 2.69 | 9.71 | -2.66 | 7.2% |
| depth 65 to 80% | 94 | 56% | 8.80 | 3.18 | 29.65 | -7.02 | 13.5% |
| ellipse ratio below 0.7 | 245 | 71% | 4.15 | 1.78 | 8.92 | -3.17 | 6.5% |
| ellipse ratio 0.7 to 0.85 | 156 | 84% | 3.15 | 1.02 | 7.36 | -1.83 | 5.0% |
| ellipse ratio 0.85 and up | 99 | 88% | 1.99 | 0.87 | 5.60 | -0.16 | 3.2% |
| camera 1.5 to 3 m away | 172 | 80% | 2.71 | 0.99 | 5.63 | -1.50 | 4.5% |
| camera 3 to 6 m away | 328 | 77% | 3.67 | 1.45 | 8.93 | -2.35 | 5.7% |
| tyre width under 15% of diameter | 249 | 80% | 3.03 | 1.49 | 7.70 | -1.88 | 5.0% |
| tyre width 15 to 30% | 251 | 76% | 3.65 | 1.18 | 7.59 | -2.23 | 5.6% |
| water visible behind the wheel | 246 | 81% | 4.13 | 1.72 | 8.75 | -2.52 | 6.5% |
| top of wheel hidden by bodywork | 92 | 49% | 5.05 | 1.72 | 14.61 | -3.96 | 7.6% |

**Cases declined, by reason**

| Reason | Cases |
|---|---|
| `arc_too_short` | 83 |
| `ambiguous_outline` | 12 |
| `waterline_below_wheel` | 5 |
| `implausible_ellipse` | 4 |
| `no_water_contact` | 2 |
| `ellipse_fit_failed` | 1 |
| `wheel_too_small` | 1 |

**Accuracy against the visible-arc threshold** (main method)

| Minimum visible arc | Usable | MAE cm | Median cm | P90 cm | MAE / diameter |
|---|---|---|---|---|---|
| 100 degrees | 78% | 3.34 | 1.25 | 7.68 | 5.3% |
| 140 degrees | 59% | 3.13 | 1.02 | 7.68 | 4.8% |
| 180 degrees | 35% | 1.64 | 0.71 | 4.28 | 2.7% |
| 220 degrees | 14% | 0.74 | 0.45 | 1.62 | 1.2% |

**Uncertainty calibration** (depth_sigma_cm: resampled measurement noise plus the calibrated model term, on seeds disjoint from the calibration)

| Measure | Value |
|---|---|
| median depth_sigma_cm | 5.35 |
| share with abs error within 1 sigma (ideal 68%) | 85% |
| share with abs error within 2 sigma (ideal 95%) | 95% |
| median abs error / sigma | 0.26 |
| share with true depth above depth + 1.5 sigma (ideal 6.7%) | 7.9% |
| share with true depth below depth - 1.5 sigma (ideal 6.7%) | 0.0% |
| median geometry time per case | 1900 ms |

<!-- synth-eval:end -->

**Reading the results.**

* "Usable" is the share of all 500 cases measured; the rest were declined with a reason, which is the intended behaviour when too little wheel shows. The outline-only row is scored on the cases the main method measured.
* The rim matters: without it, errors roughly double, because the near sidewall is hard to pin down from the tyre outline alone.
* Accuracy falls with depth, since less arc is visible. The visible-arc table shows the trade-off between declining more photos and accuracy; the service keeps 100 degrees and lets the sigma grow instead.
* Before the bias correction the method read low on average, mostly because of a tail of short-arc cases where the fit settles on a near sidewall that is too small and too high (section 4). The median correction removes the typical offset; the tail is not removed but is now covered by the sigma at the 1.5 sigma blocking line. The errors stay one-sided, so the two-sided coverage rows overstate the risk on the high side and the low-side row is the one that matters for routing.
* Only the main method is bias-corrected; the perspective and outline-only rows are raw, for comparison with each other.
* The tables were fitted on seed 9945 and checked on seed 4242, which neither the calibration nor this table used (500 cases, 387 usable, 40 draws). Before the change that seed read 2.69 cm low on average (median 0.93 cm low, MAE 3.17 cm) and 17.1 percent of true depths lay above `depth + 1.5 sigma`; after it, 1.55 cm low on average (median 0.13 cm low, MAE 2.80 cm) and 5.7 percent, with the median sigma going from 2.58 to 5.08 cm. The wider sigma is the honest price of the heavy low tail.
* The tables were calibrated with Monte Carlo sigmas. With the batched refits that the service now uses (and the 1.14 scale), the same held-out seed 4242 gives 387 usable cases, MAE 2.80 cm, bias 1.55 cm low, a median sigma of 5.07 cm, 87 percent of errors within one sigma, 95 percent within two, and 5.9 percent of true depths above `depth + 1.5 sigma` (`uv run python ../eval/synthetic.py --cases 500 --seed 4242 --draws 100`). The coverage still holds.
* "Median geometry time per case" in the table is measured with six cases running in parallel on an eight-core laptop, so it is slower than the single-process figures in section 4.
* The perspective correction did not help on these noisy masks, so it stays off by default.

## 8. Sources

* A. Fitzgibbon, M. Pilu, R. B. Fisher, "Direct least square fitting of ellipses", IEEE TPAMI 21(5), 1999.
* R. Halir, J. Flusser, "Numerically stable direct least squares fitting of ellipses", WSCG 1998.
* Meta Model API documentation: Segmenting with prompts, Reading segmentation output, Client libraries, Image understanding (perception grounding on a 0 to 1000 grid), Pricing (https://dev.meta.ai/docs).
* S. E. Rantz et al., Measurement and Computation of Streamflow, USGS Water-Supply Paper 2175, 1982.
* WMO, Manual on Stream Gauging, WMO-No. 1044, 2010.
* A. Hauet, T. Morlot, L. Daubagnan, E3S Web of Conferences 40, 06015, 2018, doi:10.1051/e3sconf/20184006015.
* Tyre sizes and kerb heights: see `perception/src/neermattam_perception/references.py`, which cites each source and marks secondary sources.

## 9. A person as the reference

Most Indian flood photos show someone wading, and many show no tyre at all. A person is a much worse ruler than a wheel, but a usable one, so the service measures against people too and labels the result `method: person_reference`. Code: `perception/src/neermattam_perception/person.py`.

### 9.1 The measurement

1. The scene model boxes each person standing in the water and gives a point on the body, the top of the head, the posture (standing, walking, wading, bending, sitting, swimming, carrying something on the head), whether they are an adult, and the body landmark at the waterline (ankle, shin, knee, mid-thigh, hip, waist, chest, shoulder, neck).
2. SAM outlines the person from the box, the body point and the head top, with water pixels beside the box as negative points. The mask is cut to the box (10 percent wider, 15 percent higher for hair), water pixels are removed, and the largest connected piece is kept.
3. The top row of the mask is the top of the head. The bottom row, where the mask meets the water, is the waterline on the body. There must be water just below it, or the person is declined (legs hidden behind a car are not a waterline). SAM often leaves the churned water at a person's legs out of the verified water mask, so when the verified water does not reach a person, water is segmented in a band just below them (points in the band, a negative point on the legs, the result kept inside the band), as is done below wheels; `reasons` says when this local, unverified water was used. If the water as a whole could not be verified, people are still measured this way.
4. The scale comes from the body itself. Going down the mask, the row width follows the head, narrows at the neck (or, with the neck hidden by a collar or hair, holds steady within 10 percent for 40 percent of the head's width in rows), then widens at the shoulders. The first row at least 1.4 times the head width is on the slope from neck to shoulder, at or above the shoulder joint; the widening ends where the width reaches 85 percent of the widest row within half a head-to-shoulder length further down (not as far as the elbows). The shoulder joint is taken at the middle of that band, with the band's spread (its length over the square root of 12, a uniform guess) added to the uncertainty of `s`. In the Drillis and Contini segment model the shoulder sits at 0.818 of stature, so the head-to-shoulder length is `k = 0.182 H`. An outline with two separate heads above the shoulders in at least 30 percent of its rows (a neighbour joined in) is declined.
5. With `L` the visible length and `s` the head-to-shoulder length, both in pixels along the body axis, the visible share of stature is `k L / s`, and

   `depth = H (1 - 0.182 L / s)`

   with `H` the stature prior. A depth below zero or above 0.9 H is declined as implausible.
6. The landmark the scene model named gives a second, coarser reading, `share(landmark) H` (knee 0.285, mid-thigh 0.41, hip 0.530, waist 0.60, chest 0.720 of stature; shin, mid-thigh and waist are interpolated, not in the source). Their difference is reported as `waterline.agreement_cm`, and widens the sigma when it exceeds two sigma.

The ratio `L / s` cancels the camera distance and focal length, which is why the scale comes from the shoulders and not from the person's height in pixels.

### 9.2 Priors

* Stature: NCD Risk Factor Collaboration (NCD-RisC), "A century of trends in adult human height", eLife 2016;5:e13410. Country estimates for India, people born 1976 to 1996: men about 165.0 cm, women about 152.5 cm. Sex cannot be told reliably from a flood photo, so the prior is an equal mixture: mean 158.75 cm, standard deviation 9.0 cm. The source gives means only; the within-sex standard deviation of 6.5 cm is an assumption.
* Segment proportions: R. Drillis and R. Contini, Body Segment Parameters, Technical Report 1166-03, Office of Vocational Rehabilitation, New York University School of Engineering and Science, 1966, as drawn in D. A. Winter, Biomechanics and Motor Control of Human Movement, 4th ed., Wiley 2009, figure 4.1. They are average proportions from a Western sample; people differ, and the spread of the head-to-shoulder share is taken as 0.008 of stature (an assumption).

### 9.3 Uncertainty

Added in quadrature, through the partial derivatives of the depth formula:

| Term | Value |
|---|---|
| stature prior | 9.0 cm, times the submerged share |
| head-to-shoulder share | 0.008 H, times `L / s` |
| shoulder row in the mask | 2 px plus 3 percent of `s` |
| waterline row in the mask | 2 px plus 1.5 percent of `L` (ripples, soft mask edge) |
| perspective and posture | 5 percent of stature (a person turned or leaning, a camera above or below them; an assumption) |

The result is typically 10 to 15 cm. Expect plus or minus 10 to 15 cm on a real photo, and say so: this is a rough measurement, about four times less precise than a wheel.

### 9.4 When a person is declined

Bending, sitting, swimming, carrying something on the head (the head top is no longer the top of the mask), children (the adult prior does not apply), an empty or very small mask (under 60 px tall), an outline that does not span the scene model's box (under 60 percent of its height, or ending more than 20 percent of its height from the box's bottom: a fragment such as a head, whose bottom is not a waterline), no shoulder widening found (hair, a raised arm or a dupatta can hide it), two heads in one outline, no water below the bottom of the mask, or an implausible depth. The posture and the adult flag come from the scene model, which can be wrong: a man leaning on a car he is pushing was called standing in one test photo, and was declined only because his outline had no clear shoulders. The reason is returned in `reasons`.

### 9.5 Choosing between a wheel and a person

A measured wheel is used whenever there is one: its sigma is a few centimetres. When a person was measured too, the two are compared. If they differ by more than twice their combined sigma, `confidence` is halved and `reasons` says so; otherwise `reasons` says the person agrees. When only a person is measured, it is the answer; when nobody and no wheel can be measured, the scene model's own estimate is returned as `model_estimate`.

Between people, and against the scene model: each person reading is compared with what the scene model says at that person (its own depth estimate there, else the depth of the body landmark it named at the waterline, else its estimate for the street). The wheel's shallow-side guard (docs/CONTRACTS.md, `model_estimate`) is applied on both sides: a reading is set aside when the scene model's value lies outside the reading's two-sigma interval and by more than half the estimate's sigma (at least 10 cm). Readings left that agree with each other (within twice their combined sigma) are combined, weighted by one over sigma squared; because they share the stature prior, the combined sigma is never smaller than the smallest single one. Readings left that disagree with each other give way to the one the scene model supports best. When none is left, the scene model's estimate is reported as `model_estimate`, and `reasons` names each reading set aside. This came from the c1 photo: the smallest sigma alone published a person reading far deeper than the shin-deep water the scene model saw at both people, and over repeated runs the answer flipped between the two people.

### 9.6 Privacy

No face is stored or shown. Three sources of head regions are blurred (pixelated by a factor of 12, then smoothed) in every image that leaves the service, the overlay, the crops shown to the verifier and the stored copy of the photo:

* every head or face the scene model boxes anywhere in the photo (the scene prompt asks for all of them, people in the water or not), grown by 30 percent on each side;
* for each person reference, the top half of its box, reaching a quarter of the box above it (boxes are not always tight);
* for each person mask, from above its top (SAM sometimes leaves a dark head out of the mask) down to the shoulder row, or half the mask when no shoulders are found, widened by half its width.

The blurring depends on the scene model and SAM finding the people: a face that neither finds is not blurred. `tests/test_person.py` checks that a fine pattern drawn on a synthetic face is gone from the overlay and the stored file, for a measured person, a declined person, and a face boxed only by the scene model.

### 9.7 Synthetic check

`uv run python ../eval/person_synth.py --n 400 --seed 11` draws front-on figures with the same Drillis and Contini proportions and stature from the same prior, in water 15 to 120 cm deep, with the shoulder share varied by its assumed spread, a neck-to-shoulder slope starting up to 0.03 H above the shoulder line, the body turned away from the camera (widths shrunk by up to 40 percent), a slight stoop (the upper body shortened by up to 5 percent) and ragged mask edges.

| Measure | Value |
|---|---|
| figures | 400 |
| measured | 394 |
| declined | 6 |
| mean absolute error | 6.5 cm |
| bias | -4.4 cm |
| 90th percentile absolute error | 13.8 cm |
| median sigma | 11.3 cm |
| errors within 1 sigma (ideal 68%) | 87% |
| errors within 2 sigma (ideal 95%) | 100% |

Where the shoulder row is taken matters most. On the same figures, taking the first wide row gives a mean absolute error of 8.1 cm (bias 6.5 cm low), the middle of the band 6.5 cm (4.4 cm low), and the end of the band 5.3 cm (2.3 cm low). The end of the band is best here only because these figures hold their arms against the body; on real photos, arms held away from the body or a loose jacket push the end of the band down, so the service takes the middle and carries the band in the sigma. The method reads low by a few centimetres on these figures, and the sigma covers it.

This checks the algorithm (the shoulder finder, the waterline row, the arithmetic and the sigma), not how people differ from the model: the figures are drawn with the very proportions the method assumes, standing straight and facing the camera. Real people lean, turn sideways, raise their arms, wear loose clothes and stand at different distances from a tilted camera, and nobody measured the water in the real photos, so the real error is not known. The sigma is wider than the synthetic errors on purpose; it carries the perspective and posture term that the figures do not exercise.
