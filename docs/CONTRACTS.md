# Service contracts

All bodies are JSON (UTF-8). Times are RFC 3339 UTC. Coordinates are WGS84 decimal degrees. Lengths are centimetres unless a field name says otherwise. Every service exposes `GET /healthz` (200 when alive) and `GET /readyz` (200 when its dependencies are reachable, 503 with a reason otherwise).

## 1. Perception (Python, port 8301)

### POST /v1/measure
Multipart form:
- `image` (jpeg/png, required unless `video` is present)
- `video` (mp4/mov, optional; used for velocity)
- `hint_reference` (optional: `two_wheeler_wheel`, `car_wheel`, `auto_wheel`, `kerb`, `person`)
- `hint_diameter_cm` (optional float, when the reporter knows the wheel size)

Response 200:
```json
{
  "measurement_id": "m_01J...",
  "usable": true,
  "reasons": ["standing water visible", "two-wheeler wheel found"],
  "reason_codes": [{"code": "water_visible", "args": {}}, {"code": "reference_found", "args": {"kind": "two_wheeler_wheel"}}],
  "depth_cm": 23.4,
  "depth_sigma_cm": 3.1,
  "reference": {"kind": "two_wheeler_wheel", "diameter_cm": 43.4, "diameter_sigma_cm": 2.0, "source": "catalogue:90/100-10", "height_cm": null, "height_sigma_cm": null},
  "waterline": {"method": "sam_mask_intersection", "y_px": 812.5, "agreement_cm": 1.2},
  "velocity_mps": null,
  "velocity_sigma_mps": null,
  "confidence": 0.82,
  "overlay_png_b64": "...",
  "models": {"scene": "muse-spark-1.3", "masks": "sam-3.1"},
  "timings_ms": {"scene": 1400, "masks": 900, "geometry": 12},
  "cost_usd": 0.0041,
  "method": "wheel_geometry",
  "model_estimate": {"depth_cm": 30.0, "depth_sigma_cm": 20.0, "basis": "water reaches the lower half of the scooter wheels", "model": "muse-spark-open"},
  "agent_trace": [
    {"step": "segment", "target": "two wheeler wheel 1", "model": "sam-2.1-hiera-small", "input": "box [612,580,660,628] px + point [636,600]", "output": "mask 1450 px, bbox [610,578,661,626] px, score 0.91", "decision": "verify", "latency_ms": 70, "prompt_version": null, "decision_code": {"code": "verify", "args": {}}},
    {"step": "verify", "target": "two wheeler wheel 1", "model": "muse-spark-open", "input": "crop [520,488,752,720] px with the outline drawn", "output": "is_target=true, outline good, waterline visible", "decision": "accept", "latency_ms": 18000, "prompt_version": "verify-wheel@5d0c3a9e41b7", "decision_code": {"code": "accept", "args": {}}}
  ],
  "captured_at": "2015-12-02T07:56:12",
  "input_sha256": "9f2c...64 hex digits",
  "certificate": {"format": "neermattam-certificate/1", "alg": "Ed25519", "key_id": "ed25519:4c1d95a9f0e2b7d1", "payload": "{\"checks\":...}", "signature": "base64url"}
}
```
When `usable` is false, `depth_cm` is null and `reasons` explains why (no water, no reference object, blurred, ambiguous waterline).

- `method`: how `depth_cm` was obtained, in order of preference:
  - `wheel_geometry`: a measurement from a wheel of known size (docs/GEOMETRY.md).
  - `person_reference`: a measurement from an adult standing in the water, scaled by an adult stature prior (docs/GEOMETRY.md, section 9). Its sigma is typically 10 to 15 cm; the UI must say it is measured against a person.
  - `model_estimate`: neither could be measured (or a wheel measurement was set aside as too shallow, see below) and `depth_cm` is the scene model's own visual estimate, with `depth_sigma_cm` at least 20 cm or 30 percent of the estimate, whichever is larger; the UI must label it as an estimate.

  Null when `usable` is false. When a wheel and a person are both measured, the wheel is used; if they differ by more than twice their combined sigma, `confidence` is lowered and `reasons` says so.
- `reference`: for `wheel_geometry`, the wheel (`diameter_cm`, `diameter_sigma_cm`). For `person_reference`, `kind` is `person`, `diameter_cm` and `diameter_sigma_cm` are null, and `height_cm` and `height_sigma_cm` give the stature prior used. Null for `model_estimate`.
- `velocity_mps`, `velocity_sigma_mps`: only with a `video`, and only when a reference was measured on its key frame: the depth-averaged velocity (0.85 times the surface speed of the tracked debris, docs/GEOMETRY.md) and its sigma. The scale is the wheel's (`wheel_geometry`) or the person's head-to-shoulder length (`person_reference`). Null when there is no video, no measured reference, or no usable track; `reasons` then says which.
- `waterline.method`: `sam_mask_intersection` for a wheel; `person_mask_bottom` for a person (where the person's mask meets the water). `agreement_cm` compares it with the scene model's own waterline; for a person, with the depth at the body landmark (knee, waist, chest and so on) the scene model named at the waterline.
- Faces: every image the service returns (`overlay_png_b64`) or stores has the head of every person it found blurred (people the scene model or SAM found; a face neither finds is not covered, see docs/GEOMETRY.md section 9.6). The photo itself is still sent to the scene model and to SAM, which run inside the service.
- `model_estimate`: the scene model's own estimate whenever it gave one (null otherwise), reported alongside a geometry measurement too. When both exist the map uses the geometry; if they differ by more than twice their combined sigma, `confidence` is lowered and `reasons` says so. Shallow side: when the estimate lies above the wheel measurement's two-sigma upper bound (`depth_cm + 2 x depth_sigma_cm`) and by more than half the estimate's sigma, the measurement is not published; `method` is `model_estimate` and `reasons` gives the wheel's reading. Calling deep water shallow is the dangerous error, so this side is the cautious one.
- Time budget: 50 s after a request starts (a 55 s budget, 5 s kept to finish), the service starts no new model request (scene model or SAM) and stops waiting for any still running, and answers with what was verified by then; `reasons` then says the time budget ran out, and `agent_trace` has a `budget` step for each part cut short (`photo` for the checks, `people`, `water below people`, `water at wheels`). The people's SAM requests are started together with the checks, so that they are done when the checks are. With a `video`, tracking is not started after the budget either (`velocity not measured: the time budget ran out`).
- `waterline.agreement_cm`: when the scene model's waterline and the mask waterline disagree, the difference is added in quadrature to `depth_sigma_cm`, so disagreement widens the uncertainty as well as lowering `confidence`.
- `agent_trace`: every model step that led to the answer, in the order the steps happened (checks of different objects run in parallel and interleave): the scene reading, each mask proposal (`segment`), each check of a mask by the scene model (`verify`, with decision `accept`, `refine with box ...` or `give up`), each geometry attempt, the estimate, and a `budget` step when the time budget stopped open checks. With a `video`, three more steps follow: `debris` (Muse Spark Open boxes the floating objects and one fixed anchor on the key frame), `track` (SAM 2.1's video predictor follows each box forward through the clip; with SAM 3.1 the noun phrase is tracked instead) and `velocity` (the speed from the tracks, or why there is none, with how many tracks were kept and why the others were rejected). `latency_ms` is the wall time of that step. On the single-request hosted path there is one `segment` step for all objects and no `verify` steps.
- `captured_at`: EXIF `DateTimeOriginal` of the photo (RFC 3339; UTC with `Z` when the camera recorded a zone offset, otherwise local time without an offset), or null. Also sent as the `X-Captured-At` response header.
- `reason_codes`, `agent_trace[].decision_code`: the reasons and decisions as codes for translation, section 1.3.
- `agent_trace[].prompt_version`: on every step that called Muse Spark Open (the scene reading, each `verify` and `confirm`, including a check that failed), the version of the prompt it was sent: a short name, `@`, and the first 12 hex digits of the SHA-256 of the prompt's text (for a prompt with parts filled in per object, of its template). Null on steps that call no language model. The names are `scene`, `scene-hosted` (the Meta Model API path), `verify-wheel`, `verify-water`, `locate-debris` (the `debris` step of a video) and `transcribe`.
- `validate` steps: every Muse Spark Open answer passes deterministic checks before it is used (section 1.1). When a check fails, a step with `step` `validate` and `model` `validator` says what failed and what was done: `retry` (the question is asked once more), `drop ...` (one element or field is left out and the rest used) or `give up` (that target, or the whole photo, is declined). A failed check is never passed silently. No step is added when every check passes.
- `input_sha256`: lower-case hex SHA-256 of the file measured (the image; the video when no image is sent) exactly as received, before any decoding or re-encoding.
- `certificate`: the signed measurement certificate (section 1.2). The service always signs; null only when the pipeline runs without a signer (tools that call it directly). An older perception sends neither `input_sha256` nor `certificate`; clients treat both as absent.

### POST /v1/measure/stream
The same request as `POST /v1/measure`, answered as server-sent events (`Content-Type: text/event-stream`) so a client can show the agent working while it works. Requests that fail validation (413, 415, 422) get the same plain HTTP errors as `/v1/measure` before any event is sent. Then:

- `event: step` once per trace step, the moment it happens; `data` is one `agent_trace` item (`step`, `target`, `model`, `input`, `output`, `decision`, `latency_ms`). Checks of different objects run in parallel, so steps of different targets interleave.
- `event: result` once at the end; `data` is exactly the `POST /v1/measure` response body. Its `agent_trace` holds the same steps in the same order. The stream then ends.
- `event: error` instead of `result` when a model is unavailable; `data` is `{"status": 503, "detail": "model unavailable: ..."}`. The stream then ends.
- A comment line (`: keep-alive`) every 15 seconds while nothing else is sent.

```
event: step
data: {"step": "scene", "target": "photo", "model": "muse-spark-open", "input": "photo 2048x1536 px", "output": "water visible; references: two_wheeler_wheel; own estimate 20 cm", "decision": "segment and verify 1 wheel and the water", "latency_ms": 9800}

event: result
data: {"measurement_id": "m_01J...", "usable": true, ...}
```

### POST /v1/transcribe
Multipart `audio` (wav/ogg/m4a/mp3, at most 25 MB). The voice note may be in any of Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, Urdu or English, mixed freely. Returns:

```json
{"text": "वेलाचेरी मेन रोड पे घुटने तक पानी है", "language": "hi", "languages": ["hi"],
 "place_text": "Velachery Main Road", "place_spoken": "वेलाचेरी मेन रोड",
 "depth_words": "घुटने तक", "depth_words_en": "knee deep",
 "depth_cm": 45.0, "depth_sigma_cm": 13.5, "depth_low_cm": 35.0, "depth_high_cm": 55.0,
 "depth_basis": "phrase table: knee (hi)", "model": "muse-spark-open", "cost_usd": 0.004,
 "input_sha256": "5be1...64 hex digits", "prompt_version": "transcribe@0a7e51c2d9f3", "validation": []}
```

- `input_sha256` is the SHA-256 of the audio file exactly as received; `prompt_version` is as in `agent_trace`.
- `validation` lists what the deterministic checks of the model's answer rejected (section 1.1), one plain sentence each, such as `language "xx" is not a supported language code; dropped`; empty when every check passed. An answer with no usable transcript after the retries is a 503, as before.

- `text` is the transcript in the scripts spoken.
- `language` is the detected language as ISO 639-1 codes (`hi`, `ta`, `te`, `kn`, `ml`, `bn`, `mr`, `gu`, `pa`, `or`, `ur`, `en`), joined with `+` for code-switching, main language first (for example `ta+en`); `languages` is the same as a list.
- `place_text` is the place normalised to the usual Latin-script spelling (for geocoding); `place_spoken` is the place as spoken. Both are empty when no place was named.
- `depth_words` are the depth words as spoken; `depth_words_en` is their English gloss.
- `depth_cm`, `depth_sigma_cm`, `depth_low_cm` and `depth_high_cm` come from the documented phrase table (`perception/src/neermattam_perception/voice.py`: ankle, shin, knee, thigh, waist, chest and neck deep in every supported language), or from a spoken number with a unit, or, when neither matches, from the model's own reading; `depth_basis` says which. The sigma is at least 10 cm or 30 percent of the depth. All four are null when no depth was spoken.

Returns 503 with a reason when the audio model is unavailable.

### GET /v1/keys
The public keys that verify this service's certificates: `{"keys": [{"key_id": "ed25519:4c1d95a9f0e2b7d1", "alg": "Ed25519", "public_key": "<base64url of the 32-byte key>"}]}`. `key_id` is `ed25519:` and the first 16 hex digits of the SHA-256 of the 32 public key bytes. The private key is the 32-byte seed in `NEERMATTAM_SIGNING_KEY` (base64url, in `.env`); without it the service makes a key for the life of the process and logs that certificates will not verify after a restart. The key is never logged or returned.

### 1.1 Checks on every model answer
Muse Spark Open's answers are checked before anything uses them (`perception/src/neermattam_perception/validate.py`):

- **Schema**: the answer is a JSON object; each field has its type (boolean, number, string, list, object or null as the prompt asks).
- **Grid**: every point and box coordinate lies on the 0 to 1000 grid; a box is `[x_min, y_min, x_max, y_max]` with `x_min < x_max` and `y_min < y_max`. Coordinates are no longer clamped onto the grid silently.
- **Inside the image**: every box, once in pixels, lies inside the image (or the crop shown).
- **Allowed values**: reference kinds, vehicle kinds, vehicle models, postures, body landmarks, positions, the verifier's `choice` (`A`, `B` only when two outlines were shown, or `none`), `outline_quality`, `view`, `coverage` and language codes come from fixed lists; shares and confidences lie in 0 to 1; depths are finite and between 0 and 1000 cm, with a low bound no higher than the high bound.

A field the decision rests on (`water_visible` in the scene reading; `is_target` and `outline_quality` in a wheel check, and `choice` when two outlines were shown; `is_water` and `coverage` in a water check; `text` in a transcript) failing its check makes the question be asked once more; failing again declines it: the photo is answered unusable with the reason, or that wheel or the water is given up. Any other field or list element that fails is dropped and named in a `validate` step. A scene answer that fails is never cached.

### 1.2 Measurement certificate
Each `/v1/measure` answer carries a certificate: a compact record of what was measured and how, signed with Ed25519, so anyone holding the public key can check that a published depth came from this service unchanged, and recompute it.

```json
{"format": "neermattam-certificate/1", "alg": "Ed25519", "key_id": "ed25519:4c1d95a9f0e2b7d1",
 "payload": "<canonical JSON text of the record below>",
 "signature": "<base64url, no padding: the 64-byte Ed25519 signature of the UTF-8 bytes of payload>"}
```

`payload` is a JSON string holding the record's JSON text, written with keys sorted, no spaces and every non-ASCII character escaped (Python `json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`). The signature covers exactly those bytes, so a verifier never re-serialises the record. The record:

- `format`: `neermattam-certificate/1`; `measurement_id`; `issued_at` (RFC 3339 UTC).
- `input`: `kind` (`image` or `video`), `sha256` (as `input_sha256`), `width` and `height` of the image measured, in pixels.
- `models`: `[{"role": "scene" | "masks", "name", "version"}]`, the model and the exact weights or endpoint and settings used.
- `prompts`: `[{"id": "scene@...", "sha256": "<64 hex>"}]`, every prompt version the steps used.
- `steps`: per `agent_trace` step, `step`, `target`, `model`, `prompt_version`, `decision` and `latency_ms` (the model outputs themselves stay in `agent_trace`).
- `checks`: `rules` (the names of the checks of section 1.1 that ran) and `failures` (what they rejected; empty when none).
- `geometry`: the numbers the depth was computed from, each block null when not used:
  - `wheel`: `target`; `reference` (`kind`, `diameter_cm`, `diameter_sigma_cm`, `source`, `squash_cm`, `squash_sigma_cm`); `ellipse` (`cx`, `cy`, `a`, `b`, `theta`: the fitted near sidewall in image pixels, `theta` in radians); `waterline` (`point`, `direction`, `water_normal`, `rms_px`, `n_points`); `camera` (`focal_px`, `cx`, `cy`, or null for the affine model); `arc_covered_deg`; `fraction_raw` (the submerged fraction `f` before the bias correction), `fraction_bias`, `fraction`; `ambiguity_spread`; `resample_sigma_cm` (the bootstrap spread of the depth, from the outline points, which are not stored); `model_sigma_share`; `depth_cm` and `depth_sigma_cm` (the wheel's own, before the tyre squash and the second waterline); `second_waterline` (`point`, `direction`, or null); `agreement_cm`; `estimate_here_cm` (the scene model's estimate the shallow-side guard compared with, or null).
  - `person`: `target`; `top_y`, `shoulder_first_y`, `shoulder_full_y`, `bottom_y` (image rows of the outline); `height_cm`, `height_sigma_cm`; `visible_share`; `depth_cm`, `depth_sigma_cm`; `landmark`, `landmark_depth_cm`, `agreement_cm`.
  - `estimate`: `depth_cm`, `low_cm`, `high_cm`, `sigma_cm`, `basis`.
- `decision`: `usable`, `method`, `depth_cm`, `depth_sigma_cm`, `confidence`, `reasons`, as in the response.

Full-precision numbers are kept in `geometry` so `python -m neermattam_perception.replay <certificate.json>` can check the signature and recompute the depth and sigma with the service's own geometry code and no model call (docs/VERIFY.md). The masks are not in the certificate; the bootstrap part of a wheel's sigma is recorded, and everything built on it is recomputed.

### 1.3 Reason and decision codes

`reasons` and each `agent_trace[].decision` are English sentences. Next to them, for clients that show another language, `reason_codes` holds one `{"code", "args"}` per reason, in the same order, and each step has `decision_code` (also on the `step` events of the stream). The English text stays, unchanged, for compatibility. The catalogue below is the whole set, kept in `perception/src/neermattam_perception/codes.py`; a test fails when the pipeline produces a sentence without a code, or when this list and the catalogue differ. Codes are stable: a changed template keeps its code if its meaning is the same, and a new meaning gets a new code.

- `args` values are numbers where the template has a number, otherwise strings. `kind` is a `RefKind` value (`two_wheeler_wheel`, `car_wheel`, `auto_wheel`, `bus_wheel`, `kerb`, `person`).
- `detail`, `basis` and `label` carry text a client cannot translate: a verifier's verdict, an image issue or the estimate's basis written by the scene model, a vehicle's catalogue name, or an error. Show them as they are.
- `target` (in `wheel_not_confirmed`) is the trace target, such as `two-wheeler wheel 1` or `car 1 wheel`.
- A sentence with no code would get `{"code": "other", "args": {"text": ...}}`. The service does not produce one; a client should show `text`.

Reasons (`reason_codes[].code`):

| Code | English template | Args |
|---|---|---|
| `water_visible` | standing water visible |  |
| `no_water` | no standing water visible |  |
| `image_unusable` | image is too blurred or dark to measure |  |
| `image_issue` | image issue: {detail} | `detail` |
| `scene_answer_rejected` | the scene model's answer failed its checks twice ({detail}) | `detail` |
| `reference_not_supported` | depth from a {kind} is not supported yet; photograph a vehicle wheel | `kind` |
| `no_reference` | no wheel or person found to measure against |  |
| `water_not_located` | could not locate the water surface |  |
| `light_truck_car_wheels` | a light truck on car-size wheels: generic car wheel size used |  |
| `no_wheel_size_for_vehicle` | no wheel size is known for a {vehicle} | `vehicle` |
| `budget_ran_out` | the time budget of {budget_s} s ran out before every check was done; this is the best answer reached in time | `budget_s` |
| `people_not_measured_budget` | people were not measured: the time budget ran out |  |
| `water_below_people_budget` | water below people not segmented: the time budget ran out |  |
| `velocity_budget` | velocity not measured: the time budget ran out |  |
| `water_partial_budget` | water: only the part confirmed before the time budget ran out is used |  |
| `water_not_confirmed` | water mask not confirmed ({detail}) | `detail` |
| `water_surface_unverified` | the water surface as a whole could not be verified |  |
| `water_not_segmented` | water could not be segmented |  |
| `wheel_not_confirmed` | {target}: the scene model did not confirm the mask ({detail}) | `target`, `detail` |
| `wheel_not_segmented` | {kind} could not be segmented (it may be fully submerged) | `kind` |
| `reference_found` | {kind} found | `kind` |
| `wheel_size_catalogue` | wheel size from catalogue: {label} | `label` |
| `wheel_size_generic` | wheel size unknown, generic prior used |  |
| `tyre_squash` | tyre squash of {squash_cm} cm subtracted (sigma {sigma_cm} cm) | `squash_cm`, `sigma_cm` |
| `waterline_agrees` | scene model waterline agrees within {cm} cm | `cm` |
| `estimate_disagrees` | the scene model's own estimate ({estimate_cm} cm) disagrees with the measurement by more than two sigma; confidence lowered | `estimate_cm` |
| `person_agrees_with_wheel` | a person in the water gives {depth_cm} cm (sigma {sigma_cm} cm), consistent with the wheel | `depth_cm`, `sigma_cm` |
| `person_disagrees_with_wheel` | a person in the water gives {depth_cm} cm (sigma {sigma_cm} cm), more than two sigma from the wheel; confidence lowered | `depth_cm`, `sigma_cm` |
| `shallow_not_trusted` | the {kind} measured {depth_cm} cm (sigma {sigma_cm} cm), but the scene model estimates {estimate_cm} cm {where}, above the measurement's two-sigma bound; a shallow reading that disagrees is not trusted, so the scene model's estimate is reported | `kind`, `depth_cm`, `sigma_cm`, `estimate_cm`, `where` |
| `estimate_only` | no wheel could be measured; depth is the scene model's visual estimate, not a measurement |  |
| `estimate_basis` | estimate based on: {basis} | `basis` |
| `person_measured` | a person standing in the water was measured against the adult stature prior ({height_cm} cm, sigma {sigma_cm} cm) | `height_cm`, `sigma_cm` |
| `person_coarse` | a person reference is coarse: expect about 10 to 15 cm of error |  |
| `person_landmark` | the scene model puts the water at the {landmark}, about {depth_cm} cm ({apart_cm} cm apart) | `landmark`, `depth_cm`, `apart_cm` |
| `person_contradicted` | person {person}: measured {depth_cm} cm (sigma {sigma_cm} cm), but the scene model sees about {scene_cm} cm there; not used | `person`, `depth_cm`, `sigma_cm`, `scene_cm` |
| `person_local_water` | person {person}: the water just below was segmented locally, not checked by the scene model | `person` |
| `persons_combined` | people {people} agree: {depth_cm} cm (sigma {sigma_cm} cm), their readings combined | `people` (such as `1 and 2`), `depth_cm`, `sigma_cm` |
| `person_declined` | person {person}: {failure} | `person`, `failure` (a sub-code below), `detail` (optional) |
| `velocity_measured` | surface velocity {surface_mps} m/s (sigma {surface_sigma_mps}) from {used} tracked objects, {rejected} rejected; depth-averaged {mean_mps} m/s (sigma {mean_sigma_mps}) | `surface_mps`, `surface_sigma_mps`, `used`, `rejected`, `mean_mps`, `mean_sigma_mps` |
| `velocity_measured_tracks` | surface velocity from {used} tracked objects | `used` |
| `velocity_no_scale` | velocity unavailable: the person gives no scale |  |
| `velocity_unavailable` | velocity unavailable: {failure} | `failure` (a sub-code below), `detail` (optional) |
| `flow_no_frame_rate` | the video has no frame rate |  |
| `flow_locator_failed` | the debris locator failed: {detail} | `detail` |
| `flow_nothing_floating` | the scene model found nothing floating to track |  |
| `flow_nothing_distinct` | no box from the scene model held an object distinct from the water |  |
| `flow_tracking_failed` | tracking failed: {detail} | `detail` |
| `wheel_failed` | {kind}: {failure} | `kind`, `failure` (a sub-code below), `detail` (optional) |

Step decisions (`agent_trace[].decision_code.code`):

| Code | English template | Args |
|---|---|---|
| `accept` | accept |  |
| `accept_partial` | accept (coverage partial) |  |
| `refine` | refine with box {box} px, then optionally ` + ` and the points | `box`, `points` (optional) |
| `give_up` | give up |  |
| `give_up_reason` | give up: {detail} | `detail` |
| `verify` | verify |  |
| `verify_best` | verify the best {n} | `n` |
| `measure` | measure |  |
| `candidate` | candidate |  |
| `declined` | declined |  |
| `rejected` | rejected |  |
| `retry` | retry |  |
| `drop` | drop |  |
| `drop_fields` | drop {n} field(s) that failed, use the rest | `n` |
| `snap` | snap |  |
| `scene_stop_no_water` | stop: no water |  |
| `scene_stop_no_wheel` | stop: no wheel |  |
| `scene_stop_unchecked` | stop: no checked scene reading |  |
| `scene_report_estimate` | no wheel: report own estimate |  |
| `scene_measure_persons` | no wheel: measure {n} person(s) against the stature prior and segment the water | `n` |
| `scene_verify_wheels` | segment and verify {n} wheel(s) and the water | `n` |
| `scene_search_vehicles` | search {vehicles} vehicle box(es) for wheels, optionally `, check {wheels} wheel(s)`, and segment the water | `vehicles`, `wheels` (optional) |
| `join_water` | join to the verified water |  |
| `check_water_below` | check that each outline ends at water |  |
| `measure_people` | measure each against the stature prior |  |
| `use_measurement` | use the measurement |  |
| `use_wheel` | use the wheel |  |
| `use_person` | use the person |  |
| `use_person_n` | use person {person} | `person` |
| `combine_people` | combine the people who agree |  |
| `report_estimate` | report model estimate |  |
| `shallow_not_trusted` | shallow reading not trusted: report the estimate |  |
| `person_not_published` | do not publish a person measurement |  |
| `budget_answer` | answer with what was verified in time |  |
| `budget_skip_people` | skip the people |  |
| `budget_keep_water` | keep the verified water as it is |  |
| `budget_verified_water_only` | use the verified water only |  |
| `track` | track |  |
| `flow_stop_nothing_floating` | stop: nothing floating to track |  |
| `check_tracks` | check the tracks |  |
| `subtract_camera_motion` | subtract its motion from every track |  |
| `camera_motion_kept` | camera motion not removed |  |
| `report_velocity` | report velocity |  |
| `no_speed` | no speed |  |
| `no_speed_rejected` | no speed ({rejected} tracks rejected) | `rejected` |

Wheel geometry failures (`wheel_failed`):

| Sub-code | English |
|---|---|
| `wheel_mask_empty` | wheel mask is empty |
| `wheel_too_small` | wheel is too small in the image to measure |
| `no_water_contact` | water does not meet the wheel |
| `ellipse_fit_failed` | could not fit the wheel outline |
| `implausible_ellipse` | fitted wheel outline is implausible |
| `arc_too_short` | too little of the wheel rim is visible above water |
| `waterline_ambiguous` | waterline on the wheel is ambiguous |
| `waterline_below_wheel` | waterline lies below the wheel |
| `fully_submerged` | wheel appears fully submerged |
| `ambiguous_outline` | visible part of the wheel fits several wheel sizes |

Person declines (`person_declined`):

| Sub-code | English |
|---|---|
| `posture` | the person is not standing upright |
| `child` | the person looks like a child; the stature prior is for adults |
| `mask_empty` | the person could not be outlined |
| `too_small` | the person is too small in the photo to measure |
| `no_shoulders` | the shoulder line could not be found |
| `merged` | the outline joins more than one person at the head |
| `box_mismatch` | the outline does not match the person the scene model boxed |
| `no_water_below` | the person's outline does not end at the water |
| `implausible` | the measured depth is outside the plausible range |

Velocity failures (`velocity_unavailable`; `other` with `detail` for a model or tracker error):

| Sub-code | English |
|---|---|
| `no_frame_rate` | frame rate unknown |
| `tracks_too_short` | no floating object was tracked for long enough |
| `nothing_moved` | no tracked object moved measurably |
| `no_common_direction` | tracks move in no common direction |
| `tracks_disagree` | tracks disagree on the flow |

## 2. Engine (Rust, port 8302)

The engine covers a configured list of cities (by default `chennai`, `mumbai`, `bengaluru`, `hyderabad`, `kolkata`, `delhi` and `visakhapatnam`), each with a metro bounding box and its own road graph. The hex grid is shared; a city scopes queries and picks the road graph. Every error is `{"error": "..."}`: 400 for invalid input (the message names the field), 404 for an unknown cell or when no road connects the points, 413 for an oversized body, and 503 while a city's road graph is loading or has failed.

### POST /v1/readings
```json
{"reading_id": "r_...", "lat": 12.9791, "lon": 80.2209, "depth_cm": 23.4, "depth_sigma_cm": 3.1,
 "velocity_mps": 0.4, "velocity_sigma_mps": 0.2, "observed_at": "2026-10-28T10:15:00Z",
 "source": "photo|video|voice|official", "trust": 0.9}
```
Accepted when `lat`,`lon` fall inside any covered city's box. Returns `{"cell": "8a61...", "city": "chennai", "depth_cm": 21.0, "depth_sigma_cm": 2.4, "hazard": {...}}`.

### GET /v1/cells?bbox=minLon,minLat,maxLon,maxLat&city=<id>
At least one of `bbox` and `city`; with both, a cell must be in both. `bbox` selects by cell centre; `city` selects the cells on the city's map (centre in the box widened by three cell edge lengths). Returns a GeoJSON FeatureCollection of hex cells, each with these properties:
- `city` (the city holding the cell's centre, else the city whose box widened by three cell edge lengths holds it, or null)
- `depth_cm`, `depth_sigma_cm`, `velocity_mps`
- `hazard_rating`, `hazard_class` (`low`, `danger_some`, `danger_most`, `danger_all`, `unknown`)
- `n_readings`, `updated_at`

### GET /v1/route?from=lat,lon&to=lat,lon&vehicle=pedestrian|two_wheeler|auto|car|bus|truck
Planned on the graph of a city whose box holds both ends. A request with an end outside every city, or with ends in two different cities, is a 400 saying so. Returns a GeoJSON LineString Feature with these properties:
- `city`, `distance_m`, `eta_min`
- `max_depth_cm_on_route`, `cells_avoided`, `unknown_cells_crossed`
- `advisory: true`, `explanation`

The baseline shortest route (ignoring water) is returned alongside it for comparison.

### GET /v1/stream?city=<id>
Server-sent events. Each event is a changed cell: `event: cell`, with data the same as one feature above. With `city`, only cells on that city's map are sent: those whose centre lies in its box widened by three cell edge lengths, so every cell a reading inside the city touches is included.

### GET /v1/cities
`{"cities": [{"id": "chennai", "name": "Chennai", "bbox": [80.10, 12.80, 80.33, 13.24], "centre": {"lat": 13.02, "lon": 80.215}, "cells": 120, "graph": {"status": "ready|loading|failed", "nodes": 316734, "edges": 709504, "memory_mib": 17.6, ...}}]}`. A failed graph carries `error` instead of the counts.

### GET /v1/hotspots?city=<id>&limit=<1..200>
Cells with evidence in force, ranked by a 0 to 100 score (`city` optional, `limit` default 20). Returns `{"city", "advisory": true, "score": "<formula>", "count", "hotspots": [...]}`; each hotspot has `cell`, `lat`, `lon`, `score`, `parts` (`depth`, `hazard`, `freshness`, `reports`), `depth_block_cm`, `depth_cm`, `depth_sigma_cm`, `hazard_class_upper`, `age_h`, `independent_reports`, `unknown`, `blocks` (vehicles the cell is closed to), `reasons` (plain-English sentences, one per part plus notes) and `place` (the nearest named road in the city's road graph within 400 m: `{"name", "distance_m", "source": "osm_road"}`, or null). `independent_reports` counts readings in force (direct readings plus adjacent cells reporting), not people. The formula is in docs/ENGINE.md.

### GET /v1/summary?city=<id>
`city` optional. Returns `cells`, `by_class` (`low`, `danger_some`, `danger_most`, `danger_all`, `unknown`), `unknown`, `stale`, `expired`, `cells_in_conflict`, `conflicting_readings`, `blocked` (cells closed to each vehicle), `needs_fresh_report` with `needs_fresh_report_cells` (stale or expired cells last seen deeper than 15 cm; at most 50, deepest first, each with `cell`, `lat`, `lon`, `last_depth_cm`, `last_reading_age_h`, `expired`), `readings`, `newest_reading_at`, and `history_points` (`total` and `max`: depth history points held over the whole map and the cap).

### GET /v1/recession?cell=<H3 index>
A cell's depth history: `history` (its latest direct readings, oldest first, each with `at`, `reading_cm`, `reading_sigma_cm`, `fused_cm`, `fused_sigma_cm`, `migrated`), `current` (the cell's properties as in `/v1/cells`), `drain` (`rate_cm_per_h`, `rate_sigma_cm_per_h`, `trend` of `falling|rising|steady`, `points_used`, `from`, `to`, `hours_to_15_cm`) or null, `drain_note` (how it was fitted, or why there is none), and `advisory: true`. 404 when no report has touched the cell. The drain rate never changes routing.

### GET /v1/export?city=<id>&format=geojson|csv
`city` required, `format` default `geojson`. GeoJSON is the `/v1/cells` collection for the city; CSV has one row per cell with the columns `cell,city,lat,lon,depth_cm,depth_sigma_cm,depth_upper_cm,depth_sigma_meas_cm,conflict_high_mean_cm,velocity_mps,velocity_sigma_mps,velocity_assumed,hazard_rating,hazard_class,hazard_class_upper,debris_factor,unknown,expired,n_readings,n_neighbour,conflicts,updated_at,last_depth_cm,last_reading_age_h` (empty for null). Sent as an attachment; the `X-Neermattam-Truncated` header says whether the cell cap cut it short and `X-Neermattam-Total-Cells` gives the city's cell count. When the cap applies, the cells kept are the highest by hotspot score, in that order. The GeoJSON also carries top-level `truncated` and `total_cells`.

### Critical sites (docs/SITES.md)

A critical site is a place whose people and supplies must keep moving in a flood. `kind` is one of `data_centre`, `hospital`, `telephone_exchange`, `substation` or `cable_landing_station`. Sites come from two places and never from typing:

- OpenStreetMap, extracted by `scripts/extract_sites.py` into `engine/assets/sites-osm.json` (embedded in the binary): `source` is `osm`, the id is `osm:<node|way|relation>:<osm id>` and `osm` is the OSM object (`way/1536001837`).
- An operator, through the gateway's developer API: `source` is `operator`, the id is `op_` plus 16 hex characters, and `owner` is the API key id that added it. The engine only ever shows an operator site to its owner.

Fuel stations (`amenity=fuel`, from the same extract) are supply points for access routes, not sites. The `truck` vehicle (a diesel tanker or heavy goods vehicle) exists for access routes and is accepted by `/v1/route` too; its limit is in docs/ENGINE.md.

A site's **watch zone** is the set of map cells whose centre lies within `radius_m` of the site. Each zone has:
- `radius_m`, `cells` (cells in the zone), `cells_reported` (with evidence in force), `cells_known` (not unknown), `coverage` (`cells_known / cells`, 0 to 1);
- `status`: `over` when any cell with evidence in force has a blocking depth (the depth routing judges a cell at, docs/ENGINE.md rule 2) over `threshold_cm`; else `under` when `coverage` is at least `min_coverage`; else `unknown`. `under` is never called safe: it means the cells with recent reports are below the threshold;
- `deepest`: the cell with the deepest blocking depth among cells with evidence in force (`cell`, `depth_block_cm`, `depth_cm`, `depth_sigma_cm`, `unknown`, `age_h`), or null;
- `old_deep_cells`, `old_deep_max_cm`, `old_deep_age_h`: cells whose evidence has expired but whose last level was over the threshold (treat as flooded until someone reports);
- `newest_evidence_age_h` (null when no cell has evidence in force);
- `trend`: `rising` when any zone cell's drain fit (as `/v1/recession`) is rising, else `falling` when at least one is falling and none is steady, else `steady`, or null when no cell has enough readings; `trend_cells` counts `rising`, `falling` and `steady`.

A site's `status` combines its zones (smallest first): `over` when the smallest zone is over; `nearby` when only a larger zone is over; `under` when the smallest zone is under and no zone is over; else `unknown`.

Query parameters shared by the site endpoints: `rings` (radii in metres, comma-separated, 1 to 3 values from 100 to 3000, default `300,1000`), `threshold_cm` (1 to 200, default 15, the lowest vehicle limit) and `min_coverage` (0 to 1, default 0.5).

#### GET /v1/sites?city=<id>&kind=&owner=&limit=&offset=
`city` required. Lists OpenStreetMap sites in the city and, with `owner`, that owner's sites too, each with `zones` and `status`. Order: `over`, `nearby`, `under`, `unknown`, then deepest first, then name. `limit` 1 to 500 (default 100). Returns `{"city", "kind", "threshold_cm", "rings_m", "min_coverage", "total", "count", "offset", "counts": {"<kind>": {"osm", "operator"}}, "osm": {"attribution", "data_until"}, "advisory": true, "sites": [...]}`. Each site has `id`, `kind`, `name`, `operator`, `lat`, `lon`, `city`, `cell`, `source`, `osm` (OSM sites), `owner` and `added_at` (operator sites), `status` and `zones`.

#### GET /v1/sites/{id}?owner=&access=true|false
One site, as in the list, plus:
- `access` (unless `access=false`): `{"advisory": true, "computed_at", "routes": [...]}` with one route per pair of target (`main_road`, then `fuel`) and vehicle (`truck`, `two_wheeler`, `car`). Each route has `target`, `vehicle`, `status` (`open`: a water-aware route exists; `no_safe_route`: every route crosses water too deep for the vehicle, the least-bad one is given; `unavailable`, with `reason`: `routing_loading`, `routing_failed`, `no_road_near_site`, `no_target_in_range` or `no_path`), `destination` (`name`, `lat`, `lon`, and `id` for a fuel station), `distance_m`, `baseline_distance_m`, `detour_m` (route minus the shortest route to the same destination, ignoring water), `eta_min`, `cells_on_route`, `unknown_cells_crossed`, `impassable_cells_crossed`, `max_known_depth_cm`, `explanation` (the engine's route text) and `coordinates` (`[lon, lat]` pairs). A main road is a node on a `motorway`, `trunk`, `primary` or `secondary` way (or their links) that the vehicle may use; a fuel station with `fuel:diesel=no` is skipped. Up to three of the nearest destinations (main road within 3 km, fuel within 8 km, at least 300 m apart) are tried and the best open route is kept.
- `risk`: the risk ledger (below) for the H3 area holding the site and the six around it: `{"area_resolution", "since", "wet_threshold_cm", "note", "areas": [...]}`.

404 for an unknown id, or an operator site asked for without its `owner`.

#### POST /v1/sites
`{"owner": "<api key id>", "kind", "name", "lat", "lon", "operator": null}`. `owner` is 1 to 64 characters of `A-Z a-z 0-9 _ -`; `name` 1 to 120 printable characters; the point must be inside a covered city. At most 100 sites per owner (`ENGINE_SITES_PER_OWNER`) and 10,000 in all. Returns 201 with the site. Operator sites are kept beside the cell snapshot, in `<snapshot>.sites.json` (`cells.sites.json` by default).

#### DELETE /v1/sites/{id}?owner=
204. 404 when the id is unknown or not the owner's; 400 for an OpenStreetMap site.

#### GET /v1/siting?city=<id>&bucket=reported|unattributed&limit=
The risk ledger for siting: per H3 area (resolution 8 by default, about 0.7 square kilometres), what direct readings have shown since the ledger began. Each area has `area`, `lat`, `lon`, `boundary` (`[lon, lat]` ring), `readings`, `wet_readings` (fused level at or over `wet_threshold_cm`, 15), `days_with_water` (distinct India Standard Time dates with a wet reading), `peak_cm`, `peak_sigma_cm`, `peak_at`, `episodes` (wet spells seen to end: the first wet reading to the first reading below the threshold in the same cell), `median_recession_h`, `max_recession_h`, `open_episodes` (wet now), `unresolved_episodes` (reports stopped while still wet, so the end was never seen), `first_at` and `last_at`. Areas are ordered by `days_with_water`, then `peak_cm`, then `wet_readings`; `limit` 1 to 500 (default 100). Top level: `city`, `bucket`, `since`, `history_days`, `area_resolution`, `wet_threshold_cm`, `total`, `advisory: true` and a `note` that the ledger only holds what has been reported since `since` and that an area with no reports is unknown, not dry. `bucket=reported` (the default) counts readings that carry a `source`; `unattributed` counts readings with none (the gateway's simulated scenario and tests), which are never mixed into `reported`. The ledger is kept beside the cell snapshot in `<snapshot>.ledger.json` (`cells.ledger.json`) and is not trimmed by the 48-hour forget window.

### GET /readyz (engine)
200 once the primary city's road graph (`ENGINE_PRIMARY_CITY`, default chennai) is loaded, with `status` `ready`, or `degraded` when another city's graph is loading or has failed (named in `reason`). 503 with a reason while the primary graph is loading or has failed. The body lists every city's graph status under `cities`.

## 3. Gateway (Go, port 8080, the only public service)

Every page takes `?lang=` (one of `en`, `ta`, `hi`, `te`, `kn`, `ml`, `bn`, `mr`, remembered in the `nm_lang` cookie) and, where it shows a city, `?city=` (an engine city id, remembered in the `nm_city` cookie; default `chennai`). Every JSON error is `{"error": "...", "code": "..."}` with `code` stable and `error` a plain sentence. Public endpoints are rate limited per client address and answer 429 with `Retry-After`. Everything about water is advisory.

### Pages (HTML, rendered on the server)
- `GET /`: landing page with a live preview of the selected city.
- `GET /map?city=&at=lat,lon`: the live map. With `at` and no `city`, the city holding the point is chosen.
- `GET /report?city=&at=`, `POST /report`: report water. The point must lie in the selected city (or, with no city sent, in any covered city).
- `GET /route?city=&from=&to=&vehicle=`, `GET /route/plan` (JSON: `summary_html` and `route`): water-aware routes inside one city.
- `GET /alerts?city=`: "Watch my street": pick a point or the last planned route and a vehicle, then turn on notifications.
- `GET /control?city=&cell=`: the control room. Needs the `nm_ctl` cookie; without it, `GET /control` shows the key entry page. `POST /control/login` (form field `key`) sets the cookie, `POST /control/logout` clears it. `GET /control/login` and `GET /control/logout` answer 303 to `/control` (keeping `lang`). When `CONTROL_ROOM_KEY` is unset the keyed control room is off and only the demo exists.
- `GET /control/demo?cell=`: the public read-only control room over the simulated Chennai scenario, labelled as such. Only with `DEMO=1`.
- `GET /control/panel?city=&cell=` (and `/control/demo/panel?cell=`): JSON `{"html": "..."}` with the live parts of the control room re-rendered, for the page's refresh glue. Same access rules as the page.
- `GET /control/export?city=&format=geojson|csv` (and `/control/demo/export?format=`): the engine's `/v1/export` passed through as an attachment, with `X-Neermattam-Truncated` and `X-Neermattam-Total-Cells`.
- `GET /sites?city=&kind=&site=`: "Critical sites": OpenStreetMap data centres (and, with `kind=hospital`, hospitals) in the city, each with its watch-zone status card, on a map with the live cells; with `site=<id>`, that site's zones, access routes for a tanker, a two-wheeler and a car, and its area's flood record. Operator sites never appear here. Linked from the control room.
- `GET /api/sites/{id}?city=`: JSON for one OpenStreetMap site (the engine's detail), rate limited like the public API.
- `POST /api/sites/alerts`: JSON `{"subscription": <PushSubscription.toJSON()>, "site": "<id>", "city", "lang", "threshold_cm"}` (threshold 5 to 100, default 15). Watches one OpenStreetMap site: a message when its 300 m zone goes over the threshold, when water over it appears within 1 km, when an access route for the tanker, the two-wheeler or the car closes or opens again, and when it is back under. Same endpoint rules, token and errors as `/api/alerts`. `GET` and `DELETE /api/sites/alerts/{id}` with `X-Alert-Token`.
- `GET /developers`: the developer API documentation and terms.
- `GET /verify/{measurement_id}`: "Verify this measurement": whether the stored certificate's Ed25519 signature checks out against the published key, whether its input hash matches the file the gateway received, what the gateway did with the measurement (added to the map with its trust, refused with the refusing checks' reasons, no depth, or not recorded for certificates kept before decisions were), and the certificate in readable form. A refused measurement is titled "Signature verified, but not added to the map". 404 when the gateway holds no certificate for that id (an older perception, an unusable voice note, or an id it never saw).
- `GET /about`: how it works, the hackathon it was built in, the repository and the deployed commit (read from `BUILD_INFO_PATH`, default `/opt/neermattam/BUILD_INFO`).
- Any other path: a 404 page in the page language with links to the main pages; paths under `/api/`, `/v1/`, `/webhooks/`, `/.well-known/` and `/static/`, and requests whose `Accept` names JSON and not HTML, get the JSON error with `code` `not_found`.
- `GET /sw.js`: the service worker that shows push notifications (served from the root so its scope covers the site).

### Public JSON API (rate limited per client address)
- `GET /api/cities`: `{"cities": [{"id", "name", "bbox", "centre", "cells", "routing": "ready|loading|failed", "simulated_scenario"}], "default": "chennai"}`.
- `GET /api/cells?city=<id>` or `?bbox=minLon,minLat,maxLon,maxLat` (default: the default city): the engine's cells for the city, compacted, each with `properties.city` and `properties.origin` (`reported`, `simulated` or `mixed`), plus top-level `city` and `simulated_scenario`. 2-second cache, `ETag`, 304.
- `GET /api/route?from=lat,lon&to=lat,lon&vehicle=pedestrian|walking|two_wheeler|auto|car|bus`: the engine's route Feature with its `baseline`. Both ends must lie in one covered city. Engine 400 and 503 answers are passed on with a plain message and `code` `outside_area`, `different_cities` or `routing_unavailable`.
- `GET /api/stream?city=<id>`: server-sent events: `hello`, `cell` (one compacted cell on that city's map), `report` (a ticker item with `city`), `resync`, and a ping comment every 20 seconds. Without `city`, every city.
- `POST /api/report`: multipart with `image`, `video` or `audio`, `lat`, `lon`, optional `city`, `hint_reference`, `hint_diameter_cm`. Runs perception, verification and the engine; returns the measurement, the verification and the cell update. When perception sent a certificate, the reply has `certificate_url` (`/verify/{measurement_id}`) and `measurement.certificate` is left out (it is served on its own, below).
- `GET /api/overlay/{measurement_id}`: the report overlay JPEG, kept for an hour.
- `GET /api/certificates/{measurement_id}`: the certificate exactly as perception signed it (the gateway's decision is kept beside it on disk, outside the signed bytes, and is not part of this answer) (section 1.2), as `application/json`, plus `X-Neermattam-Verified: true|false`. Certificates are kept in `GATEWAY_DATA_DIR/certificates` (the newest 20,000; in memory, the newest 2,000, when persistence is off). 404 `no_certificate`.
- `GET /.well-known/neermattam-keys.json`: the public keys certificates are checked against, in the shape of perception's `GET /v1/keys`, with `"source"` per key (`config` for `CERTIFICATE_PUBLIC_KEYS`, `perception` for keys fetched from perception). The gateway trusts only these keys, never a key carried in a certificate, and keeps the keys it has fetched in `certificate-keys.json`, so certificates signed before a key change still verify.
- `GET /api/alerts/key`: `{"vapid_public_key": "<base64url P-256 point>"}`, or 404 when push alerts are off.
- `POST /api/alerts`: JSON `{"subscription": <PushSubscription.toJSON()>, "city", "vehicle", "lang", "point": {"lat","lon"}}`, or with `"route": {"from": "lat,lon", "to": "lat,lon"}` in place of `point`: the gateway plans the water-aware route for that vehicle and watches every map cell along it (sampled every 25 m, at most 800 points). The endpoint must be https on a known push service (`PUSH_HOSTS`). Returns 201 `{"id", "token", "status": "over|under|unknown", "class", "depth_cm", "where", "message", "expires_at"}`. The `token` is needed to read or delete it. 400 `bad_endpoint`, `bad_keys` or `bad_watch`; 409 `too_many` when the endpoint already watches `ALERTS_PER_CLIENT` places; 503 `full` when the gateway holds `ALERTS_MAX`; 404 `alerts_off`.
- `GET /api/alerts/{id}` and `DELETE /api/alerts/{id}`, with header `X-Alert-Token`: read its status or stop it.

### Developer API (API key)
`Authorization: Bearer <key>` (or `X-API-Key`). Keys are created with `go run ./cmd/neermattam-keys create`; only their SHA-256 is stored. Every answer carries `X-RateLimit-Limit` and `X-RateLimit-Remaining` (requests per minute for that key). 401 `missing_key` or `invalid_key`, 403 `key_disabled`, 429 `rate_limited`.
- `GET /api/v1/route?from=lat,lon&to=lat,lon&vehicle=`: as `/api/route`, plus top-level `advisory: true` and `terms`. Counted as one route for billing.
- `GET /api/v1/cells?city=<id>` (or `bbox`): as `/api/cells`, plus `advisory` and `terms`; `ETag` and 304.
- `GET /api/v1/sites?city=<id>&kind=&rings=&threshold_cm=&min_coverage=&limit=&offset=`: the engine's `/v1/sites` with `owner` set to the key, so the answer holds the OpenStreetMap sites and this key's own sites, plus `advisory` and `terms`.
- `GET /api/v1/sites/{id}`: one site with its watch zones, access routes and area risk (the engine's `/v1/sites/{id}` for this key).
- `POST /api/v1/sites`: JSON `{"kind", "name", "lat", "lon", "operator"}`; adds a site owned by this key. 201 with the site; 400 `bad_site` with the engine's reason; 409 `too_many_sites`.
- `DELETE /api/v1/sites/{id}`: removes one of this key's sites. 204; 404 `no_site`.
- `GET /api/v1/siting?city=<id>&limit=`: the engine's `/v1/siting` (reported readings only), plus `advisory` and `terms`.
- Site reads are counted as `sites` in usage.
- `GET /api/v1/usage`: this key's own counts: `{"key": "<prefix>", "name", "rate_per_min", "total": {"routes", "cells"}, "days": [{"date", "routes", "cells"}]}` (last 90 UTC days).

### WhatsApp
- `GET /webhooks/whatsapp`: Meta's verify handshake. `POST /webhooks/whatsapp`: signed with `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret). A photo, video or voice note plus a location is a report. The text `watch` (optionally with a vehicle, such as `watch car`) followed by a location watches that point, with the same rules as push alerts; `stop` ends every watch for the number. Only when the three WhatsApp variables are set.

### Operations
- `GET /healthz`: 200 while the process runs.
- `GET /readyz`: 200 with `status` `ready`, or `degraded` when the engine is degraded (some city's routing is loading or failed, named in `reason`); 503 when the engine or perception is not ready. Each probe has `READY_PROBE_TIMEOUT` (5 s). A probe that times out uses that service's last successful probe when it is younger than `READY_GRACE` (30 s) and adds `engine_note` or `perception_note` saying so; a refused connection or an error status is reported at once.
- `GET /metrics`: Prometheus text format.
