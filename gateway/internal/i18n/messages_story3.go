package i18n

// Text for the landing page blocks added later on 3 November 2026: the
// architecture, travellers, why these Meta models fit the design, and the
// plan beyond floods and beyond India. English is here; every other
// language is in messages_story3_<code>.go, merged with mergeStory2.

func init() { mergeStory2("en", story3En) }

var story3En = map[string]string{
	// Architecture
	"lp3.a_title": "How the parts connect",
	"lp3.a_lead":  "A report goes in once and comes out everywhere it is needed. Each service can be run and checked on its own.",
	"lp3.a_in":    "In",
	"lp3.a_in1":   "Photo or short video",
	"lp3.a_in2":   "Voice note",
	"lp3.a_in3":   "Developer API",
	"lp3.a_gw":    "Gateway",
	"lp3.a_gw_p":  "Pages in eight languages, upload checks, rate limits, API keys and the live stream to every map.",
	"lp3.a_pc":    "Perception agent",
	"lp3.a_pc1":   "Muse Spark Open and SAM 2.1 check each other",
	"lp3.a_pc2":   "Geometry gives the depth and its error",
	"lp3.a_pc3":   "Guards refuse what cannot be trusted",
	"lp3.a_pc4":   "A signed certificate for every result",
	"lp3.a_en":    "Fusion and routing engine",
	"lp3.a_en1":   "Map cells about 66 m on a side, fused over time",
	"lp3.a_en2":   "Routes for each vehicle around water",
	"lp3.a_en3":   "The flood record for siting",
	"lp3.a_out":   "Out",
	"lp3.a_mcp":   "MCP server for AI assistants",
	"lp3.f13_p":   "Any AI assistant can ask Neermattam for water depth near a point, a safe route for a vehicle, or a site's water and access status.",

	// Travellers
	"lp3.w_trav":    "Visitors and travellers",
	"lp3.w_trav_p":  "A visitor who does not know which streets flood gets routes around the reported water, in English or in one of seven Indian languages.",
	"lp3.save_trav": "Saves a wrong turn into water that a local would have known to avoid.",

	// Why these Meta models fit
	"lp3.g_title": "What this design needs, and why Meta's open models fit it",
	"lp3.g1":      "Pixel outlines, because a ruler needs edges",
	"lp3.g1_p":    "The depth comes from where the waterline meets a wheel or a person. SAM 2.1 draws that outline to the pixel. A chat model alone answers in words or boxes, not outlines.",
	"lp3.g2":      "A judge that can see",
	"lp3.g2_p":    "Muse Spark Open reads images and reasons about them, so it can look at each SAM outline on a close crop and accept, reject or redraw it.",

	// Beyond floods, beyond India
	"lp3.b_eyebrow": "The plan ahead",
	"lp3.b_title":   "Beyond floods, beyond India",
	"lp3.b_lead":    "This is the plan, not something built. The same photo-to-measurement agent and street map can carry other street hazards, and other flood-prone cities.",
	"lp3.b_plan":    "Plan, not built",
	"lp3.b_h1":      "Other street hazards",
	"lp3.b_h1a":     "Waterlogging after any heavy rain",
	"lp3.b_h1b":     "Road damage and potholes",
	"lp3.b_h1c":     "Fallen trees and blocked roads after cyclones",
	"lp3.b_h1d":     "Landslide debris on hill roads",
	"lp3.b_h2":      "Other flood-prone cities",
	"lp3.b_h2_p":    "Nothing is trained per city, so a new city needs its road map and its people's reports. Cities that flood often, for example:",
	"lp3.b1_big":    "1.81 billion",
	"lp3.b1":        "people worldwide, 23% of the world's population, are directly exposed to 1-in-100-year floods.",
	"lp3.b2_big":    "390 million",
	"lp3.b2":        "of them live in India. With China, India accounts for over a third of global exposure.",
	"lp3.b3_big":    "USD 2 trillion",
	"lp3.b3":        "in economic losses from flooding worldwide since 2000, and more than USD 42 billion in 2025 alone.",
}
