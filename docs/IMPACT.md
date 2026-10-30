# Why Neermattam matters, who uses it, and how it grows

## The problem, in one street
Every northeast monsoon, Chennai's streets fill with water whose depth nobody can see from the outside. A rider on a scooter, an auto driver with a fare, a parent walking a child home: each of them has to guess whether the water ahead is ankle deep or knee deep. A wrong guess drowns an engine, strands a family or worse.

The city does have flood forecasting, and it is good at answering "where will it flood?". It does not answer the question the person at the water's edge has: "how deep is it here, right now, and can my vehicle get through?". Public crowd tools exist, but they ask people to judge depth by eye.

Neermattam answers that question with a measurement. A phone photo of a wheel already standing in the water becomes a depth in centimetres with an honest uncertainty, because a wheel has a known size and Meta's models can find it, outline it and check their own work. Many such measurements become a live map, and the map becomes a route that bends around water each kind of vehicle should not cross.

## Who benefits
| Who | What they get |
|---|---|
| Commuters and families | A route for their own vehicle, or on foot, with the water it avoids shown plainly |
| Two-wheeler and auto drivers | The vehicles most at risk in shallow water get their own depth limits |
| Delivery and ride-hailing riders | Fewer drowned engines and abandoned orders; routes that stay usable |
| Ward and city control rooms | A street-level depth layer from the public, with uncertainty and age on every cell, to dispatch pumps and close the right roads |
| Researchers and planners | A record of where water stood, how deep and for how long, street by street |
| Data-centre, hospital and telecom operators | Water near each site, whether tankers and staff can still reach it, and where water has stood for choosing the next site (docs/SITES.md) |

## Why it is built the way it is
- **Measure, do not guess.** The depth comes from geometry on a reference of known size, not from a model's impression. When no wheel can be measured, the answer is labelled as a model estimate with a wide band, never passed off as a measurement.
- **Verify every step.** An agent proposes, segments, checks and refines, and every step is shown to the user with its model, decision and time. A measurement can be traced end to end.
- **Never call water safe on weak evidence.** Old reports do not silently drain away, conflicting reports widen the uncertainty, and an area with no reports is unknown, not safe. Routes are always advisory.
- **Open models, local language.** Meta's open-weight models run on partner infrastructure, voice reports work in Indian languages, and the whole system can be self-hosted by a city.

## The data it produces over time
Every accepted report adds a dated, located depth with its uncertainty and the evidence behind it. Over one monsoon this becomes something that does not exist today in public form:
1. **Street-level depth history:** for each 66 m cell, when water arrived, how deep it got, and how long it stayed.
2. **Recurring trouble spots:** underpasses and low roads that flood at lower rainfall than their neighbours, ranked by how often and how deep.
3. **Recession behaviour:** how fast each area drains after rain stops, which points to blocked or undersized drains.
4. **Calibration for forecasts:** observed depths that forecast models can be checked against, street by street.

Personal data is not part of this. Photos are measured and discarded, GPS metadata is stripped, and only the depth, its uncertainty, the location the reporter chose and the time are kept.

## How it can sustain itself
These are directions, not claims of revenue. Each would need pilots and agreements.

| Path | Who pays | What they pay for |
|---|---|---|
| City and disaster-management deployments | Municipal corporations, state disaster authorities | A hosted or self-hosted control-room layer, integration with existing dashboards, staff training |
| Fleet and mobility routing | Delivery, ride-hailing and logistics companies | A routing API that avoids water by vehicle type, priced per request or per fleet |
| Motor insurance | Insurers handling flood claims | Independent, dated evidence of water depth at a place and time, with the measurement trace |
| Data licences | Urban planners, researchers, real-estate and infrastructure firms | The depth-history archive, anonymised and aggregated |

The public map and reporting stay free. A sensible first milestone is one ward pilot with a city control room during a monsoon, measuring how many reports arrive, how quickly cells refresh and whether dispatch decisions change.

## Data-centre operators

These are directions, not claims of revenue; no operator has been approached or has paid.

India's cloud capacity is being built now by the largest operators: Google's AI hub in Visakhapatnam at about USD 15 billion over 2026 to 2030, Microsoft's USD 17.5 billion for India over 2026 to 2029, AWS's USD 8.3 billion for its Mumbai Region by 2030, and a 168 MW data centre in Jamnagar that Reliance will build and Meta will lease. Chennai is one of the main hubs, with clusters in Ambattur and Siruseri, and press coverage of the 2025 XDI report put more than one in ten Tamil Nadu data centres already at high physical climate risk. Each figure, with its date and source, is in docs/SITES.md.

A siting or operations team asks: which parcels flood, how deep and how often, and can we reach the site in a flood. The "Critical sites" view (docs/SITES.md) answers with what Neermattam already measures:

| Question | What it gives | What it is not |
|---|---|---|
| Is water coming at the site? | Live depth within 300 m and 1 km, with sigma, freshness, trend and "unknown" | A gauge at the site, or a forecast |
| Can staff and fuel reach it? | Water-aware routes to the nearest main road and fuel station for a tanker, a two-wheeler and a car, with alerts when one closes | A guarantee that a road is passable |
| Where should the next one go? | Per area: days with water, peak depth, time to go down, from reports since the record began | A flood frequency study; it builds up over a monsoon |

What an operator might pay for: private sites and alerts through the developer API, an export of its sites' records for its own risk team, and, during a monsoon, the same ward-level evidence a city control room sees. The free public page lists only OpenStreetMap sites; an operator's own sites stay private to its key. A pilot would need an operator willing to share where its staff and tankers actually go, and would be judged by whether the access routes matched what its drivers found.

## What we expect from Meta's tooling, and what we already use
- **Muse Spark Open** reads scenes, points at reference objects, checks masks and understands voice notes in Indian languages.
- **SAM** turns those points into pixel-exact outlines of wheels and water, and tracks floating debris in video for speed.
- **Open weights on partner infrastructure** mean a city can run the whole stack without depending on a single hosted API.
- When the hosted Meta Model API is available to us, Muse Spark 1.3, SAM 3.1 and Muse Voice Transcribe switch on behind the same interfaces.

## How we will know it works
- **Accuracy:** depth error against ground truth (published in `eval/`), reported honestly with its uncertainty coverage.
- **Coverage:** reports per hour and the share of affected cells with fresh evidence during a rain event.
- **Usefulness:** routes requested, the share that avoided reported deep water, and control-room feedback.
- **Safety:** the number of routes that crossed water later reported deeper than the vehicle's limit. The target is zero, and every case gets investigated.
