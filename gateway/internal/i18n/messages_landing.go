package i18n

// Text for the landing page rewrite of 1 November 2026: the India-wide
// hero, the recorded showcase run, how Meta's models make it work in any
// city, the live numbers for every city, the sample photos a visitor can
// measure in one tap, the depth figure on the result card, and the
// Critical sites page's table of sites with no recent reports. English is
// here; the other languages are in messages_landing_<code>.go. Keys that
// already existed (meta.*, title.home, home.preview_img) are given new
// wording here and override the older files.

var enLanding = map[string]string{
	"meta.description":    "Neermattam measures floodwater depth on Indian streets from one phone photo, fuses reports into a live map for seven cities, and routes each vehicle around water it cannot cross.",
	"meta.og_description": "Street-level flood depth from one photo, in seven Indian cities. A live depth map and water-aware routes. Advisory only.",
	"title.home":          "Neermattam, street-level flood depth for Indian cities",
	"home.preview_img":    "Schematic map of the chosen city with the current flood cells coloured by depth",

	// Hero
	"lp.eyebrow":     "Live street flood depth in seven Indian cities",
	"lp.h1_a":        "Forecasts say where India will flood.",
	"lp.h1_b":        "Neermattam measures how deep, street by street.",
	"lp.lead":        "One phone photo of a wheel or a person standing in the water becomes a depth in centimetres, with an honest plus or minus. Meta's open models read the photo and check their own work, and a live map routes riders, autos, cars, buses and people on foot around water they cannot cross.",
	"lp.cta_measure": "Measure a photo now",
	"lp.cta_map":     "See the live map",
	"lp.try_label":   "No flood photo with you? Tap one of ours and watch it measured live:",
	"lp.stat_cities": "cities with live maps and routing",
	"lp.stat_pages":  "languages on every page",
	"lp.stat_voice":  "languages understood in voice notes",
	"lp.stat_signed": "Every reading signed and replayable",

	// Showcase
	"lp.sc_kicker":        "One real measurement",
	"lp.sc_run":           "Recorded run, %s",
	"lp.sc_alt_photo":     "A flooded street in %s, with the people's faces blurred",
	"lp.ph_read":          "Read the scene",
	"lp.ph_outline":       "Outline the water",
	"lp.ph_check":         "Check every outline",
	"lp.ph_measure":       "Measure",
	"lp.sc_alt_person":    "Close-up of the person who was measured: the outline in violet, the height line in yellow and the waterline in blue",
	"lp.sc_ruler_person":  "Measured against a person standing in the water, taken as an adult of %s cm, plus or minus %s.",
	"lp.sc_ruler_wheel":   "Measured from a wheel of known size, where the waterline cuts across it.",
	"lp.sc_also":          "Muse Spark Open's own estimate from the scene was %s cm, plus or minus %s.",
	"lp.sc_verdicts_aria": "What Muse Spark Open decided about each outline",
	"lp.sc_verdicts":      "%d outlines judged:",
	"lp.sc_caption":       "%s, %s. Photo:",
	"lp.sc_honest":        "Faces blurred. Nobody measured the water that day, so this shows how the system works, not how accurate it is.",
	"lp.t_water":          "Water",
	"lp.t_wheel":          "Wheel",
	"lp.t_person":         "Person",
	"lp.t_other":          "Outline",
	"lp.thumb_alt_water":  "Step %d: the water outline SAM 2.1 proposed, tinted on the photo, as Muse Spark Open saw it",
	"lp.thumb_alt_wheel":  "Step %d: two candidate wheel outlines, A and B, on a close crop, as Muse Spark Open saw them",
	"lp.thumb_alt_person": "Step %d: the person outline, as Muse Spark Open saw it",
	"lp.thumb_alt_other":  "Step %d: the outline Muse Spark Open was shown",

	// How Meta's models make it work anywhere
	"lp.meta_eyebrow":     "Built on Meta's open models",
	"lp.meta_title":       "How Meta's models make it work anywhere in India",
	"lp.meta_lead":        "Nothing here was trained on one city. Two open-weight Meta models do all the seeing, and on every photo one checks the other's work.",
	"lp.flow_photo":       "A phone photo",
	"lp.flow_photo_p":     "or a short video or voice note",
	"lp.flow_read":        "reads the scene: water, wheels, people, faces",
	"lp.flow_outline":     "outlines the wheel, the person and the water",
	"lp.flow_judge":       "judges every outline, rejects or refines it",
	"lp.flow_geometry":    "Geometry",
	"lp.flow_geometry_p":  "turns the waterline into a depth and its plus or minus",
	"lp.flow_cert":        "Signed certificate",
	"lp.flow_cert_p":      "every step and number, replayable",
	"lp.why1":             "No per-city training",
	"lp.why1_p":           "SAM 2.1 outlines wheels, people and water in streets it has never seen, guided by the boxes and points Muse Spark Open gives it. A lane in Kolkata needs nothing a lane in Chennai did not.",
	"lp.why2":             "A model that checks its own work",
	"lp.why2_p":           "Muse Spark Open looks at each outline on a close crop and accepts it, rejects it or sends SAM a corrected box, at most twice. A bad outline costs a few seconds, not a wrong depth on the map.",
	"lp.why3":             "Open weights, run anywhere",
	"lp.why3_p":           "Both models are open weights. A state disaster authority or a city corporation could run the whole pipeline on its own servers, so no citizen's photo has to leave them.",
	"lp.why4":             "Every reading can be audited",
	"lp.why4_p":           "Each measurement carries an Ed25519 signature over the photo's hash, the models, the prompts, every agent step and every number. Anyone can replay the geometry without calling a model.",
	"lp.status_running":   "Running now",
	"lp.status_running_p": "Muse Spark Open on Baseten and SAM 2.1 inside our own container, for every photo and voice note on this site.",
	"lp.status_off":       "Written, not running",
	"lp.status_off_p":     "SAM 3.1, whose weights are gated and not yet approved for us, and the hosted Meta Model API with Muse Spark 1.3, which cannot make calls until a card is on the account.",

	// Agent trace
	"lp.trace_lead":   "The same photo, step by step: %d steps in %s. Each image is exactly what Muse Spark Open was shown, with SAM 2.1's outline drawn on it.",
	"lp.trace_try":    "Run it on a photo yourself",
	"lp.raw":          "Raw numbers",
	"lp.raw_output":   "Output",
	"lp.raw_decision": "Decision",
	"lp.all_steps":    "All %d steps, with the raw numbers",
	"lp.col_step":     "Step",
	"lp.col_target":   "Target",
	"lp.col_model":    "Model",
	"lp.trace_credit": "Photo: %s, by %s, %s. Run recorded on %s with Muse Spark Open on Baseten and SAM 2.1; the model outputs are in the models' own words. Every step of the run is listed in",

	// Live across India
	"lp.india_eyebrow": "Live across India",
	"lp.india_title":   "Seven cities, each with its own map and road graph",
	"lp.india_lead":    "Chennai, Mumbai, Bengaluru, Hyderabad, Kolkata, Delhi and Visakhapatnam each have a live depth map, routing on their own OpenStreetMap roads, rainfall for the last day, and critical sites to watch. Water appears where people report it.",
	"lp.no_data":       "no data",
	"lp.none_yet":      "none yet",
	"lp.k_rain":        "Rain, last 24 h",
	"lp.k_reports":     "Reports today",
	"lp.k_deepest":     "Deepest reported",
	"lp.k_sites":       "Critical sites",
	"lp.india_src":     "Rainfall from %s, updated %s. Report counts are real reports only, never the simulated scenario.",
	"lp.india_offline": "Live rainfall and report counts could not be read just now; the maps still work.",
	"lp.india_unknown": "A street nobody has reported on is unknown, never safe.",
	"lp.photos_title":  "Photos measured so far",

	// Who it serves
	"lp.who_title":      "Built for the people who have to cross the water",
	"lp.who_riders":     "Riders and delivery fleets",
	"lp.who_riders_p":   "Two-wheelers stall first. Routes keep riders out of water that may be over 15 cm, and fleets can ask for the same routes through the API.",
	"lp.who_crews":      "Ambulances and relief crews",
	"lp.who_crews_p":    "Plan a car or bus route around water deeper than the vehicle can take, and see how many streets on the way nobody has reported on.",
	"lp.who_link_route": "Plan a route",
	"lp.who_families":   "Families",
	"lp.who_families_p": "Watch your street or the way home, and get a message when the water there crosses your vehicle's limit, or clears.",
	"lp.who_control_p":  "One live picture of street depth, with the uncertainty, source and age of every reading, and the streets that need a fresh report.",
	"lp.who_sites":      "Data centres and hospitals",
	"lp.who_sites_p":    "Know when water is within 300 m or 1 km of a site, and whether a fuel tanker can still reach it.",
	"lp.who_link_sites": "See critical sites",
	"lp.built":          "Built by one developer during the Meta Global AI Developer Hackathon, 26 October to 4 November 2026. Every line of its own code was written during the event, and it is open:",

	// Sample photos
	"lp.sample_kind_person": "People standing in the water",
	"lp.sample_kind_wheel":  "Motorcycle wheels in the water",
	"lp.sample_kind_car":    "A car in the water",
	"lp.try_eyebrow":        "Try it now",
	"lp.try_title":          "No flood photo with you? Measure one of ours",
	"lp.try_lead":           "Real flood photos from Chennai and Kolkata, with faces and number plates blurred. Tap one and it goes through the live pipeline right now, exactly as your own photo would: Muse Spark Open, SAM 2.1, the checks and a signed certificate.",
	"lp.sample_alt":         "Flood photo from %s",
	"lp.try_button":         "Measure this photo",
	"lp.try_note":           "Each tap is a fresh run, so the result can differ from the last one. A sample is never added to the live map, because its water is years old.",
	"err.unknown_sample":    "There is no sample photo with that name.",

	// Depth figure on the result card
	"fig.ankle":         "Ankle",
	"fig.knee":          "Knee",
	"fig.thigh":         "Mid-thigh",
	"fig.waist":         "Waist",
	"fig.chest":         "Chest",
	"fig.aria":          "Water at %s cm, plus or minus %s cm, drawn beside a ruler, a scooter wheel and a standing adult",
	"fig.aria_estimate": "The model's estimate of %s cm, plus or minus %s cm, drawn beside a ruler, a scooter wheel and a standing adult",
	"fig.key_band":      "The shaded band is plus or minus %s cm.",
	"fig.key_est":       "Hatched: a visual estimate, plus or minus %s cm, not a measurement.",
	"fig.key_refs":      "For scale: a scooter wheel 43 cm across and an adult of 159 cm.",

	// Critical sites
	"lp.sites_no_water":      "No water has been reported near any of these %d sites.",
	"lp.sites_no_water_p":    "That does not make them safe: it means nobody has reported from those streets recently.",
	"lp.sites_unknown_title": "%d sites with no recent reports nearby",
	"lp.sites_unknown_p":     "Unknown, not safe: nobody has reported water within 1 km of these sites recently. Open one for its access routes and flood record.",
	"lp.sites_col_site":      "Site",
	"lp.sites_col_reports":   "Street cells reported within 1 km",

	// Certificate page
	"lp.vfy_chip_sample": "Sample photo, not on the map",
}

// Added after the first screenshots: the hero's model line and the city
// tiles shown when the live numbers cannot be read.
var enLanding2 = map[string]string{
	"lp.hero_models":   "Runs on Meta's open models",
	"lp.k_cells":       "Map cells with evidence",
	"lp.k_routing":     "Routing",
	"lp.routing_ready": "Road graph ready",
	"lp.routing_wait":  "Road graph loading",
	// The report page now says a person works as a ruler too, and that
	// voice notes are understood in twelve languages.
	"report.lead":       "A wheel or a person standing in the water works as a ruler. Photograph one where it meets the water, and Neermattam measures the depth, checks the report and adds it to the live map.",
	"report.step1_hint": "Include a whole scooter, auto or car wheel side on, or a person standing in the water. Videos up to %d seconds are accepted; speed is measured from a video only when the clip allows it, as an experimental feature. No photo? Say how deep it is, in any of twelve languages, from Hindi and Tamil to Bengali and Urdu.",
}
