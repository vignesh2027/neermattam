package i18n

// Text for the landing page sections of 3 November 2026: why India needs
// street depth, everything the system does today, the industry section for
// data centres, hospitals and telecom, what each sector saves, and the
// planned pricing. English is here; every other language is in its own
// messages_story2_<code>.go, merged the same way. The sources for every
// external figure are linked beside it in templates/home.html.

func init() { mergeStory2("en", story2En) }

// mergeStory2 adds one language's strings to its catalogue.
func mergeStory2(code string, msgs map[string]string) {
	cat := Catalogs[code]
	if cat == nil {
		return
	}
	for k, v := range msgs {
		cat[k] = v
	}
}

var story2En = map[string]string{
	// Why India needs this
	"lp3.i_eyebrow": "Why India needs this",
	"lp3.i_title":   "Floods cost India every year, and the systems to act on depth already exist",
	"lp3.i_lead":    "These are national averages from India's own flood records. The people and control rooms who would use a street depth are already in place.",
	"lp3.i1_big":    "₹6,972 crore",
	"lp3.i1":        "average flood damage per year in India, 1953 to 2021.",
	"lp3.i2":        "people killed by floods in an average year, over the same period.",
	"lp3.i3_big":    "7.38 million ha",
	"lp3.i3":        "flooded in an average year, out of 40 million hectares liable to floods.",
	"lp3.i4_big":    "USD 5 billion",
	"lp3.i4":        "a year in rain-driven urban flood losses by 2030, rising to USD 30 billion by 2070, a World Bank projection of losses that adaptation can avoid.",
	"lp3.e_title":   "Who is ready to act on a measured depth",
	"lp3.e1":        "Smart Cities run an Integrated Command and Control Centre; emergency and disaster response is one of their uses.",
	"lp3.e4_big":    "Every state",
	"lp3.e4":        "has a disaster management authority under the Disaster Management Act, 2005, which plans and runs flood response.",

	// Everything it does today
	"lp3.f_eyebrow": "Everything it does today",
	"lp3.f_title":   "One photo starts it. Twelve working parts carry it to the people who need it.",
	"lp3.f_lead":    "Each card opens a page that works now. Water appears on the maps only where people have reported it.",
	"lp3.f1":        "Measure a photo",
	"lp3.f1_p":      "Depth in centimetres with its plus or minus, and every step the agent took shown beside it.",
	"lp3.f2":        "Voice notes",
	"lp3.f2_p":      "Depth words such as “knee deep” in 12 languages become a depth range. The parser is tested on 36 written phrases; speech recognition is not yet evaluated per language.",
	"lp3.f3":        "Live depth map",
	"lp3.f3_p":      "Seven cities, with live rainfall from Open-Meteo. A street with no reports stays unknown.",
	"lp3.f4":        "Routes by vehicle",
	"lp3.f4_p":      "For walking, a two-wheeler, an auto, a car and a bus, each kept out of water it should not cross.",
	"lp3.f5":        "Street alerts",
	"lp3.f5_p":      "Watch my street: a browser notification when water near it goes over the limit, gets worse or clears.",
	"lp3.f6":        "Control room",
	"lp3.f6_p":      "Hotspots ranked by depth, hazard and freshness, with CSV and GeoJSON export for ward officers.",
	"lp3.f7":        "Critical sites",
	"lp3.f7_p":      "Data centres, hospitals, telephone exchanges and cable landing stations: water within 300 m and 1 km, tanker and staff access routes, and site alerts.",
	"lp3.f8":        "Flood record and developer API",
	"lp3.f8_p":      "Where water has stood, for siting new capacity, plus routes, depth cells and sites through keyed API access.",
	"lp3.f9":        "Signed certificates",
	"lp3.f9_p":      "Every result is signed with Ed25519. Anyone can verify it and replay the geometry.",
	"lp3.f10":       "Replay a past flood",
	"lp3.f10_p":     "Cyclone Michaung, December 2023, replayed on the map and labelled simulated.",
	"lp3.f11":       "Archival flood photos",
	"lp3.f11_p":     "Fourteen street flood photos from five cities, measured and shown as their own map layer.",
	"lp3.f12":       "Eight languages",
	"lp3.f12_p":     "Every page in English, Tamil, Hindi, Telugu, Kannada, Malayalam, Bengali and Marathi.",
	"lp3.f_note":    "WhatsApp intake is written and switches on with a business number.",

	// Data centres, hospitals and telecom
	"lp3.s_eyebrow":  "For data centres, hospitals and telecom",
	"lp3.s_title":    "Know if water is coming at your site, and whether fuel and staff can still reach it",
	"lp3.s_lead":     "Mumbai and Chennai hold most of India's data centre capacity, and both flood. A site team needs the depth on the streets around its gate, not a rainfall total for the city. Neermattam turns street reports into that view for every site.",
	"lp3.s1":         "Water watch at 300 m and 1 km",
	"lp3.s1_p":       "The deepest live depth in each ring with its plus or minus, how fresh the evidence is, and whether the water is rising or falling. Thin evidence reads unknown.",
	"lp3.s2":         "Access routes for tanker and staff",
	"lp3.s2_p":       "Routes from the site to the nearest main road and fuel station for a diesel tanker (30 cm limit), a two-wheeler and a car: open, open with a detour, or no safe route.",
	"lp3.s3":         "Site alerts",
	"lp3.s3_p":       "A notification when water crosses the threshold within 300 m or 1 km, or when an access route closes or opens again.",
	"lp3.s4":         "Flood record for siting",
	"lp3.s4_p":       "For each area of about 0.7 km², how often water stood there, how deep and how long it took to go down. Real reports only, through the siting API.",
	"lp3.s5":         "API for the operations team",
	"lp3.s5_p":       "Sites, rings, routes and the record as JSON with a key. Add a site OpenStreetMap lacks; it stays private to your key.",
	"lp3.s6":         "What is watched today",
	"lp3.s6_p":       "Every site comes from OpenStreetMap. Cable landing stations appear as soon as one is mapped.",
	"lp3.s_dc":       "data centres",
	"lp3.s_hosp":     "hospitals",
	"lp3.s_tel":      "telephone exchanges",
	"lp3.s_in":       "in Chennai",
	"lp3.s_shot_k":   "Live page",
	"lp3.s_shot_alt": "The Critical sites page for Nxtra Data Chennai: both water rings unknown, and six access routes open for a diesel tanker, a two-wheeler and a car.",
	"lp3.s_shot_cap": "A screenshot of the live Critical sites page for Nxtra Data Chennai in Siruseri, 3 November 2026. No water had been reported near it, so both rings read unknown and all six routes were open.",
	"lp3.s_advisory": "Advisory only. Unknown is never safe, and no route is a guarantee.",
	"lp3.s_go_sites": "Open Critical sites",
	"lp3.s_go_api":   "Developer API",

	// What each sector saves
	"lp3.save_riders":   "Saves a drowned engine and a lost shift, by seeing the deep stretch before reaching it.",
	"lp3.save_crews":    "Saves minutes on a call, by not driving into a street the map already shows too deep.",
	"lp3.save_families": "Saves a guess at the gate: the alert says when water near home goes over the limit.",
	"lp3.save_control":  "Saves piecing the picture together from phone calls: one live list of the deepest, freshest hotspots.",
	"lp3.save_dc":       "Saves sending a tanker down a road the map already shows closed to it.",
	"lp3.save_hosp":     "Saves staff and supplies from a closed approach: the open route is found first.",
	"lp3.save_ins":      "Saves time in claims triage: signed depth evidence for a place and time.",
	"lp3.save_dev":      "Saves building a depth source from scratch: one key for routes, cells and sites.",

	// Planned pricing
	"lp3.p_label":     "Planned pricing, not yet on sale",
	"lp3.p_col_plan":  "Plan",
	"lp3.p_col_what":  "What is included",
	"lp3.p_col_price": "Planned price",
	"lp3.pd":          "Developers",
	"lp3.pd_what":     "Developer API",
	"lp3.pd_p":        "Routes, depth cells and critical sites through a key, for building and testing.",
	"lp3.pd_price":    "Free up to a monthly call limit",
	"lp3.p1_price":    "Priced per 1,000 route calls",
	"lp3.p2_price":    "Priced per site, per month",
	"lp3.p3_price":    "Priced per certificate, or by volume",
	"lp3.p4_price":    "Yearly licence and support",
}
