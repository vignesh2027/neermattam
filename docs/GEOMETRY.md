# Geometry: water depth from one photo, with a wheel as the ruler

This note explains how `perception/` turns two masks (a wheel and the water) into a depth with an uncertainty, what can go wrong, and how well it works on synthetic scenes. Code: `perception/src/neermattam_perception/geometry.py`. Every number in the results section is produced by `uv run python -m neermattam_perception.synth_eval` and can be rerun.

## 1. The idea in one paragraph

A wheel is a circle of known diameter standing in a vertical plane. Floodwater hides its lower part. The waterline across the wheel is a horizontal chord of that circle, and the water depth at the wheel is the height of that chord above the bottom of the circle. So the depth is a fraction of the diameter: `depth = f x D`. The fraction `f` is a ratio of two lengths along the same line inside one object, which is why the camera distance and the focal length drop out. SAM 3.1 gives the masks, Muse Spark 1.3 says where to look, and the geometry below does the rest.

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

`s = 1` means the water just touches the bottom (`f = 0`); `s = -1` means the wheel is fully under (`f = 1`). The overlay draws the vertical diameter from `s n` to `+n` mapped back into the image.

### 2.4 The exact perspective version

When the wheel is close or the camera is steep, the affine approximation leaves a small error, because a pinhole camera does not preserve ratios along a line. With known intrinsics `K` the error can be removed. The image ellipse `C` (a 3 x 3 conic matrix) back-projects to the cone `Q = K^T C K`. Its eigenvalues `l1 >= l2 > 0 > l3` give the circle's plane normal up to two solutions,

```
n = +/- sqrt((l1 - l2) / (l1 - l3)) e1 + sqrt((l2 - l3) / (l1 - l3)) e3
```

For each, rotate the camera virtually until that plane faces it (the homography `K R K^-1`). The ellipse becomes a circle and the construction of section 2.3 becomes exact. We keep the solution whose implied vertical, the in-plane direction perpendicular to the waterline and pointing away from the water, best matches "up" in the photo, since wheels stand upright and phones are held roughly level. `geometry.projective_fraction` implements this, and a unit test checks it is exact on a projected circle where the affine answer is off by more than 1 percent of the diameter.

The focal length comes from EXIF `FocalLengthIn35mmFilm` when present (35 mm equivalents match the 43.27 mm film diagonal, so `f_px = f35 x diagonal_px / 43.27`). On the synthetic set the correction did not beat the affine model once mask noise and fitting error were present (see the results), so the pipeline leaves it off by default (`PipelineConfig.perspective = "off"`).

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

1. **Monte Carlo over the measurement.** Each of 100 draws (in the service) bootstraps the outline points with pixel-quantisation jitter and refits the outline; bootstraps the waterline points and refits the line; shifts the line by a level error equal to the observed ripple (ripples are spatially correlated along the chord, so they do not average out); and draws a diameter from `N(D, sigma_D)`.
2. **Model ambiguity.** The fit is started from several tread hypotheses. All fits within 15 percent of the best cost are kept, and the spread of the depths they imply enters as a uniform range.
3. **Model error.** Resampling the same masks cannot reveal fit bias on short arcs, residual perspective or tyre shape. A table of model sigma against visible arc, as a share of the diameter, is calibrated on synthetic scenes with seeds disjoint from the evaluation (`synth_eval --calibrate`), targeting the 68th percentile of the absolute error so that one sigma means what it says. It is made non-increasing in arc. The calibration table lives in `geometry.MODEL_SIGMA_TABLE`.

The `confidence` field is a 0 to 1 ranking score built from the scene model's image-quality score, the relative sigma and the agreement of the two waterlines. It is not a probability.

**Second waterline.** Muse Spark independently points to where the water meets the wheel's left and right edges. That line is measured on the same fitted wheel, and `waterline.agreement_cm` is the difference between the two depths. It flags splash, reflections and mask errors that the masks alone cannot see.

## 5. Error budget

| Source | Effect | Handling |
|---|---|---|
| Mask edge noise | wobbles the outline by 1 to 2 px; biases short arcs | robust fit, orthogonal distance, Monte Carlo, model table |
| Little arc above water | the fit is poorly conditioned once less than about 150 degrees shows | declined below 100 degrees; sigma grows through the model table; the pipeline prefers the wheel with the smallest sigma, so a bigger wheel in view wins |
| Ripples and wakes | move the waterline locally by about their amplitude | robust line fit; level error in the Monte Carlo equals the observed scatter |
| Splash hiding the waterline | scattered chord points | declined when the scatter is large; the second waterline disagrees |
| Water seen behind the wheel | touches the outline above the waterline | orientation rule in step 3; only points cutting through the outline form the chord |
| Bodywork over the wheel | straight occluding edge | straight-run removal; arc check |
| Tyre width at oblique views | outline is two sidewalls | two-sidewall model, anchors, rim |
| Perspective | ratios not exactly preserved at close range | exact correction available; measured on the synthetic set |
| Wheel off vertical | a wheel leaning by `e` makes the in-plane distance `d / cos(e)`: +1.5 percent at 10 degrees, +3.5 percent at 15 degrees (a scooter on its side stand) | not corrected; part of the stated limits |
| Tyre squash | a loaded tyre's axle sits below its unloaded radius, so the fitted circle reaches below the road by the deflection and the depth reads high by it | subtracted for the auto-rickshaw (4.00-8, 1.9 cm) and the bus (10.00 R20, 3.2 cm), whose loaded radii come from tyre listings; for two-wheeler and car tyres no verified figure was found, so no correction is made and those depths may read slightly high |
| Tread wear | a worn tread is smaller by a few millimetres of radius | inside the per-tyre sigma |
| Wheel size | catalogue tyre: sigma 1.0 cm (two-wheeler), 1.5 cm (car), 2.0 cm (auto, bus); variants mixed when the position or trim is unknown; generic priors are the mixture of all catalogue wheels of the kind | carried into the Monte Carlo |

## 6. Velocity

With a video, SAM 3.1 tracks the floating debris Muse Spark named (a short noun phrase such as "plastic bottle"). Each track's centroids become a path in centimetres using the wheel's scale, `D / (2a)` cm per pixel, from the semi-major axis, which is the wheel radius without foreshortening. A Theil-Sen line of position against time gives each track's velocity, robust to a bad mask on a frame. Tracks that are too short, move against the common flow, or sit far from the median speed are dropped. Surface speed is the median of the rest.

Depth-averaged velocity is taken as 0.85 times the surface velocity: "A coefficient of 0.85 is commonly used to convert the velocity of a surface float to mean velocity in the vertical" (Rantz et al. 1982, USGS Water-Supply Paper 2175, vol. 1, p. 262, doi:10.3133/wsp2175; the same value appears in WMO 2010, Manual on Stream Gauging, WMO-No. 1044, vol. I, section 8.3). Hauet et al. (2018, E3S Web of Conferences 40, 06015, doi:10.1051/e3sconf/20184006015) found a mean of about 0.8 over 3611 LSPIV gaugings, with 5th to 95th percentiles of 0.7 and 0.9; we carry 0.06 as the coefficient's sigma. Limits: only motion in the image plane is seen; the scale is right only at the wheel's distance; a moving camera adds its own motion unless a static reference track is supplied.

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
Generated by `uv run python -m neermattam_perception.synth_eval --cases 500 --seed 2026` (40 Monte Carlo draws per case, 417 s wall time).

**All cases, by depth method** (errors over usable cases only)

| Subset | Cases | Usable | MAE cm | Median cm | P90 cm | Bias cm | MAE / diameter |
|---|---|---|---|---|---|---|---|
| tyre + rim, affine (pipeline default) | 500 | 78% | 3.78 | 1.41 | 9.35 | -3.28 | 6.0% |
| tyre + rim, perspective with EXIF focal | 500 | 78% | 4.00 | 1.48 | 9.84 | -3.19 | 6.4% |
| tyre + rim, perspective with assumed 26 mm | 500 | 78% | 4.01 | 1.60 | 9.04 | -3.20 | 6.4% |
| tyre outline only, affine | 392 | 98% | 7.21 | 4.81 | 15.70 | -7.19 | 11.5% |

**Breakdown for tyre + rim, affine (pipeline default)**

| Subset | Cases | Usable | MAE cm | Median cm | P90 cm | Bias cm | MAE / diameter |
|---|---|---|---|---|---|---|---|
| depth 5 to 25% of diameter | 128 | 90% | 0.98 | 0.53 | 1.85 | -0.38 | 1.6% |
| depth 25 to 50% | 164 | 85% | 3.06 | 1.61 | 7.81 | -2.83 | 4.8% |
| depth 50 to 65% | 114 | 74% | 5.03 | 3.04 | 11.28 | -4.38 | 8.3% |
| depth 65 to 80% | 94 | 56% | 9.79 | 4.65 | 31.74 | -8.99 | 15.1% |
| ellipse ratio below 0.7 | 245 | 71% | 4.85 | 2.17 | 10.27 | -4.47 | 7.7% |
| ellipse ratio 0.7 to 0.85 | 156 | 84% | 3.57 | 1.26 | 9.12 | -2.98 | 5.6% |
| ellipse ratio 0.85 and up | 99 | 88% | 1.96 | 0.93 | 5.14 | -1.32 | 3.2% |
| camera 1.5 to 3 m away | 172 | 80% | 3.16 | 1.38 | 7.18 | -2.69 | 5.3% |
| camera 3 to 6 m away | 328 | 77% | 4.12 | 1.47 | 10.11 | -3.59 | 6.4% |
| tyre width under 15% of diameter | 249 | 80% | 3.51 | 1.58 | 9.12 | -3.05 | 5.8% |
| tyre width 15 to 30% | 251 | 76% | 4.07 | 1.26 | 9.80 | -3.51 | 6.2% |
| water visible behind the wheel | 246 | 81% | 4.52 | 1.43 | 10.10 | -3.88 | 7.1% |
| top of wheel hidden by bodywork | 92 | 49% | 5.86 | 1.84 | 16.30 | -5.82 | 8.8% |

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
| 100 degrees | 78% | 3.78 | 1.41 | 9.35 | 6.0% |
| 140 degrees | 59% | 3.48 | 1.20 | 9.06 | 5.4% |
| 180 degrees | 35% | 1.79 | 0.78 | 4.86 | 3.0% |
| 220 degrees | 14% | 0.70 | 0.47 | 1.42 | 1.2% |

**Uncertainty calibration** (depth_sigma_cm: Monte Carlo plus the calibrated model term, on seeds disjoint from the calibration)

| Measure | Value |
|---|---|
| median depth_sigma_cm | 2.84 |
| share with abs error within 1 sigma (ideal 68%) | 68% |
| share with abs error within 2 sigma (ideal 95%) | 85% |
| median abs error / sigma | 0.58 |
| median geometry time per case | 3702 ms |

<!-- synth-eval:end -->

**Reading the results.**

* "Usable" is the share of all 500 cases measured; the rest were declined with a reason, which is the intended behaviour when too little wheel shows. The outline-only row is scored on the cases the main method measured.
* The rim matters: without it, errors roughly double, because the near sidewall is hard to pin down from the tyre outline alone.
* Accuracy falls with depth, since less arc is visible. The visible-arc table shows the trade-off between declining more photos and accuracy; the service keeps 100 degrees and lets the sigma grow instead.
* The bias is negative: on short arcs with noisy edges, the fitted ellipse tends to come out too large, so depths read low. The calibrated sigma covers it at the one-sigma level, but the tails are heavier than a normal distribution (see the two-sigma share), so a single deep reading should be treated with care and fused with others.
* The perspective correction did not help on these noisy masks, so it stays off by default.

## 8. Sources

* A. Fitzgibbon, M. Pilu, R. B. Fisher, "Direct least square fitting of ellipses", IEEE TPAMI 21(5), 1999.
* R. Halir, J. Flusser, "Numerically stable direct least squares fitting of ellipses", WSCG 1998.
* Meta Model API documentation: Segmenting with prompts, Reading segmentation output, Client libraries, Image understanding (perception grounding on a 0 to 1000 grid), Pricing (https://dev.meta.ai/docs).
* S. E. Rantz et al., Measurement and Computation of Streamflow, USGS Water-Supply Paper 2175, 1982.
* WMO, Manual on Stream Gauging, WMO-No. 1044, 2010.
* A. Hauet, T. Morlot, L. Daubagnan, E3S Web of Conferences 40, 06015, 2018, doi:10.1051/e3sconf/20184006015.
* Tyre sizes and kerb heights: see `perception/src/neermattam_perception/references.py`, which cites each source and marks secondary sources.
