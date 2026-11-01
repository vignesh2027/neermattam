package i18n

// The fixed texts perception's pipeline (not a model) puts in a reason's
// or a decision's `detail` argument, such as "time budget reached", in
// every language. internal/reasons translates a detail that is exactly one
// of these and quotes any other as the model's words; rc.details labels
// error text from a tool, which is quoted as it is too. English is
// enDetail; each language's map has the same keys.

var enDetail = map[string]string{
	"rc.details": "Details:",

	"rc.detail.time_budget_reached":         "the time budget was reached",
	"rc.detail.partial_time_budget_reached": "only partly checked: the time budget was reached",
	"rc.detail.answers_failed_checks":       "the scene model's answers failed their checks",
	"rc.detail.verification_failed":         "the check could not be completed",
	"rc.detail.confirmation_failed":         "the confirmation could not be completed",
	"rc.detail.no_wheel_proposal":           "no wheel-like outline was found in the vehicle's box",
	"rc.detail.no_prompt_on_water":          "no point was placed on the water",
	"rc.detail.outline_box_disagree":        "the outline and the tyre's box disagree",
	"rc.detail.outline_inside_rim":          "the outline follows the rim, inside the tyre",
	"rc.detail.outline_not_outer_edge":      "the outline is not the tyre's outer edge",
	"rc.detail.accepted_not_wheel_shaped":   "the scene model accepted the outline, but it is not wheel-shaped",
	"rc.detail.not_the_named_object":        "it is not the object that was named",
	"rc.detail.not_water":                   "it is not water",
	"rc.detail.no_mask":                     "no outline",
}

var taDetail = map[string]string{
	"rc.details": "விவரங்கள்:",

	"rc.detail.time_budget_reached":         "நேர வரம்பு முடிந்துவிட்டது",
	"rc.detail.partial_time_budget_reached": "பகுதியளவே சரிபார்க்கப்பட்டது: நேர வரம்பு முடிந்துவிட்டது",
	"rc.detail.answers_failed_checks":       "காட்சி மாதிரியின் பதில்கள் சோதனைகளில் தேறவில்லை",
	"rc.detail.verification_failed":         "சரிபார்ப்பை முடிக்க முடியவில்லை",
	"rc.detail.confirmation_failed":         "உறுதிசெய்யும் படியை முடிக்க முடியவில்லை",
	"rc.detail.no_wheel_proposal":           "வாகனத்தின் பெட்டிக்குள் சக்கரம் போன்ற வரைவு எதுவும் கிடைக்கவில்லை",
	"rc.detail.no_prompt_on_water":          "நீரின் மீது எந்தப் புள்ளியும் குறிக்கப்படவில்லை",
	"rc.detail.outline_box_disagree":        "வரைவும் டயரின் பெட்டியும் ஒத்துப்போகவில்லை",
	"rc.detail.outline_inside_rim":          "வரைவு டயருக்கு உள்ளே உள்ள சக்கர விளிம்பைப் பின்தொடர்கிறது",
	"rc.detail.outline_not_outer_edge":      "வரைவு டயரின் வெளி ஓரத்தைப் பின்தொடரவில்லை",
	"rc.detail.accepted_not_wheel_shaped":   "காட்சி மாதிரி வரைவை ஏற்றுக்கொண்டது, ஆனால் அது சக்கர வடிவில் இல்லை",
	"rc.detail.not_the_named_object":        "இது குறிப்பிடப்பட்ட பொருள் அல்ல",
	"rc.detail.not_water":                   "இது நீர் அல்ல",
	"rc.detail.no_mask":                     "வரைவு இல்லை",
}

var hiDetail = map[string]string{
	"rc.details": "विवरण:",

	"rc.detail.time_budget_reached":         "समय सीमा ख़त्म हो गई",
	"rc.detail.partial_time_budget_reached": "केवल आंशिक जाँच हुई: समय सीमा ख़त्म हो गई",
	"rc.detail.answers_failed_checks":       "दृश्य मॉडल के उत्तर जाँच में विफल रहे",
	"rc.detail.verification_failed":         "जाँच पूरी नहीं हो सकी",
	"rc.detail.confirmation_failed":         "पुष्टि पूरी नहीं हो सकी",
	"rc.detail.no_wheel_proposal":           "वाहन के बॉक्स में पहिए जैसी कोई रूपरेखा नहीं मिली",
	"rc.detail.no_prompt_on_water":          "पानी पर कोई बिंदु नहीं रखा गया",
	"rc.detail.outline_box_disagree":        "रूपरेखा और टायर का बॉक्स आपस में मेल नहीं खाते",
	"rc.detail.outline_inside_rim":          "रूपरेखा टायर के अंदर रिम के साथ-साथ चलती है",
	"rc.detail.outline_not_outer_edge":      "रूपरेखा टायर का बाहरी किनारा नहीं है",
	"rc.detail.accepted_not_wheel_shaped":   "दृश्य मॉडल ने रूपरेखा स्वीकार की, लेकिन उसका आकार पहिए जैसा नहीं है",
	"rc.detail.not_the_named_object":        "यह वह वस्तु नहीं है जिसका नाम दिया गया था",
	"rc.detail.not_water":                   "यह पानी नहीं है",
	"rc.detail.no_mask":                     "कोई रूपरेखा नहीं",
}

var teDetail = map[string]string{
	"rc.details": "వివరాలు:",

	"rc.detail.time_budget_reached":         "సమయ పరిమితి ముగిసింది",
	"rc.detail.partial_time_budget_reached": "కొంత భాగమే తనిఖీ అయింది: సమయ పరిమితి ముగిసింది",
	"rc.detail.answers_failed_checks":       "దృశ్య మోడల్ సమాధానాలు తనిఖీల్లో విఫలమయ్యాయి",
	"rc.detail.verification_failed":         "తనిఖీని పూర్తి చేయలేకపోయాం",
	"rc.detail.confirmation_failed":         "నిర్ధారణను పూర్తి చేయలేకపోయాం",
	"rc.detail.no_wheel_proposal":           "వాహనం బాక్సులో చక్రం లాంటి రూపురేఖ ఏదీ కనిపించలేదు",
	"rc.detail.no_prompt_on_water":          "నీటిపై ఒక్క బిందువు కూడా ఉంచలేదు",
	"rc.detail.outline_box_disagree":        "రూపురేఖ, టైరు బాక్సు ఒకదానితో ఒకటి సరిపోవడం లేదు",
	"rc.detail.outline_inside_rim":          "రూపురేఖ టైరు లోపల ఉన్న చక్రం అంచు వెంట సాగుతోంది",
	"rc.detail.outline_not_outer_edge":      "రూపురేఖ టైరు బయటి అంచు కాదు",
	"rc.detail.accepted_not_wheel_shaped":   "దృశ్య మోడల్ రూపురేఖను అంగీకరించింది, కానీ అది చక్రం ఆకారంలో లేదు",
	"rc.detail.not_the_named_object":        "ఇది పేర్కొన్న వస్తువు కాదు",
	"rc.detail.not_water":                   "ఇది నీరు కాదు",
	"rc.detail.no_mask":                     "రూపురేఖ లేదు",
}

var knDetail = map[string]string{
	"rc.details": "ವಿವರಗಳು:",

	"rc.detail.time_budget_reached":         "ಸಮಯದ ಮಿತಿ ಮುಗಿಯಿತು",
	"rc.detail.partial_time_budget_reached": "ಭಾಗಶಃ ಮಾತ್ರ ಪರಿಶೀಲಿಸಲಾಯಿತು: ಸಮಯದ ಮಿತಿ ಮುಗಿಯಿತು",
	"rc.detail.answers_failed_checks":       "ದೃಶ್ಯ ಮಾದರಿಯ ಉತ್ತರಗಳು ಪರಿಶೀಲನೆಯಲ್ಲಿ ವಿಫಲವಾದವು",
	"rc.detail.verification_failed":         "ಪರಿಶೀಲನೆಯನ್ನು ಪೂರ್ಣಗೊಳಿಸಲು ಆಗಲಿಲ್ಲ",
	"rc.detail.confirmation_failed":         "ದೃಢೀಕರಣವನ್ನು ಪೂರ್ಣಗೊಳಿಸಲು ಆಗಲಿಲ್ಲ",
	"rc.detail.no_wheel_proposal":           "ವಾಹನದ ಪೆಟ್ಟಿಗೆಯಲ್ಲಿ ಚಕ್ರದಂತಹ ಯಾವುದೇ ರೇಖೆ ಸಿಗಲಿಲ್ಲ",
	"rc.detail.no_prompt_on_water":          "ನೀರಿನ ಮೇಲೆ ಯಾವುದೇ ಬಿಂದುವನ್ನು ಇಡಲಿಲ್ಲ",
	"rc.detail.outline_box_disagree":        "ರೇಖೆ ಮತ್ತು ಟೈರ್‌ನ ಪೆಟ್ಟಿಗೆ ಹೊಂದಿಕೆಯಾಗುತ್ತಿಲ್ಲ",
	"rc.detail.outline_inside_rim":          "ರೇಖೆ ಟೈರ್‌ನ ಒಳಗಿರುವ ರಿಮ್ ಅನ್ನು ಅನುಸರಿಸುತ್ತಿದೆ",
	"rc.detail.outline_not_outer_edge":      "ರೇಖೆ ಟೈರ್‌ನ ಹೊರ ಅಂಚು ಅಲ್ಲ",
	"rc.detail.accepted_not_wheel_shaped":   "ದೃಶ್ಯ ಮಾದರಿ ರೇಖೆಯನ್ನು ಒಪ್ಪಿಕೊಂಡಿತು, ಆದರೆ ಅದು ಚಕ್ರದ ಆಕಾರದಲ್ಲಿಲ್ಲ",
	"rc.detail.not_the_named_object":        "ಇದು ಹೆಸರಿಸಿದ ವಸ್ತು ಅಲ್ಲ",
	"rc.detail.not_water":                   "ಇದು ನೀರು ಅಲ್ಲ",
	"rc.detail.no_mask":                     "ರೇಖೆ ಇಲ್ಲ",
}

var mlDetail = map[string]string{
	"rc.details": "വിശദാംശങ്ങൾ:",

	"rc.detail.time_budget_reached":         "സമയപരിധി കഴിഞ്ഞു",
	"rc.detail.partial_time_budget_reached": "ഭാഗികമായി മാത്രം പരിശോധിച്ചു: സമയപരിധി കഴിഞ്ഞു",
	"rc.detail.answers_failed_checks":       "ദൃശ്യ മോഡലിന്റെ ഉത്തരങ്ങൾ പരിശോധനകളിൽ പരാജയപ്പെട്ടു",
	"rc.detail.verification_failed":         "പരിശോധന പൂർത്തിയാക്കാൻ കഴിഞ്ഞില്ല",
	"rc.detail.confirmation_failed":         "സ്ഥിരീകരണം പൂർത്തിയാക്കാൻ കഴിഞ്ഞില്ല",
	"rc.detail.no_wheel_proposal":           "വാഹനത്തിന്റെ ബോക്സിൽ ചക്രം പോലുള്ള ഒരു രൂപരേഖയും കണ്ടെത്തിയില്ല",
	"rc.detail.no_prompt_on_water":          "വെള്ളത്തിൽ ഒരു ബിന്ദുവും അടയാളപ്പെടുത്തിയില്ല",
	"rc.detail.outline_box_disagree":        "രൂപരേഖയും ടയറിന്റെ ബോക്സും പൊരുത്തപ്പെടുന്നില്ല",
	"rc.detail.outline_inside_rim":          "രൂപരേഖ ടയറിനുള്ളിലെ റിമ്മിനെയാണ് പിന്തുടരുന്നത്",
	"rc.detail.outline_not_outer_edge":      "രൂപരേഖ ടയറിന്റെ പുറത്തെ അരികല്ല",
	"rc.detail.accepted_not_wheel_shaped":   "ദൃശ്യ മോഡൽ രൂപരേഖ അംഗീകരിച്ചു, പക്ഷേ അതിന് ചക്രത്തിന്റെ ആകൃതിയില്ല",
	"rc.detail.not_the_named_object":        "പേരു പറഞ്ഞ വസ്തു ഇതല്ല",
	"rc.detail.not_water":                   "ഇത് വെള്ളമല്ല",
	"rc.detail.no_mask":                     "രൂപരേഖയില്ല",
}

var bnDetail = map[string]string{
	"rc.details": "বিস্তারিত:",

	"rc.detail.time_budget_reached":         "সময়সীমা ফুরিয়ে গেছে",
	"rc.detail.partial_time_budget_reached": "আংশিকভাবে যাচাই হয়েছে: সময়সীমা ফুরিয়ে গেছে",
	"rc.detail.answers_failed_checks":       "দৃশ্য মডেলের উত্তরগুলো যাচাইয়ে উতরোয়নি",
	"rc.detail.verification_failed":         "যাচাই শেষ করা যায়নি",
	"rc.detail.confirmation_failed":         "নিশ্চিত করার কাজ শেষ করা যায়নি",
	"rc.detail.no_wheel_proposal":           "গাড়ির বক্সে চাকার মতো কোনো রেখা পাওয়া যায়নি",
	"rc.detail.no_prompt_on_water":          "জলের উপর কোনো বিন্দু রাখা হয়নি",
	"rc.detail.outline_box_disagree":        "রেখাটি টায়ারের বক্সের সঙ্গে মিলছে না",
	"rc.detail.outline_inside_rim":          "রেখাটি টায়ারের ভেতরে চাকার কিনারা ধরে গেছে",
	"rc.detail.outline_not_outer_edge":      "রেখাটি টায়ারের বাইরের প্রান্ত নয়",
	"rc.detail.accepted_not_wheel_shaped":   "দৃশ্য মডেল রেখাটি মেনে নিয়েছে, কিন্তু সেটি চাকার আকারের নয়",
	"rc.detail.not_the_named_object":        "যে বস্তুর নাম বলা হয়েছিল, এটি সেটি নয়",
	"rc.detail.not_water":                   "এটি জল নয়",
	"rc.detail.no_mask":                     "কোনো রেখা নেই",
}

var mrDetail = map[string]string{
	"rc.details": "तपशील:",

	"rc.detail.time_budget_reached":         "वेळेची मर्यादा संपली",
	"rc.detail.partial_time_budget_reached": "फक्त अंशतः तपासले: वेळेची मर्यादा संपली",
	"rc.detail.answers_failed_checks":       "दृश्य मॉडेलची उत्तरे तपासणीत अपयशी ठरली",
	"rc.detail.verification_failed":         "तपासणी पूर्ण करता आली नाही",
	"rc.detail.confirmation_failed":         "पुष्टी पूर्ण करता आली नाही",
	"rc.detail.no_wheel_proposal":           "वाहनाच्या बॉक्समध्ये चाकासारखी कोणतीही रेषा सापडली नाही",
	"rc.detail.no_prompt_on_water":          "पाण्यावर एकही बिंदू ठेवला नाही",
	"rc.detail.outline_box_disagree":        "रेषा आणि टायरचा बॉक्स जुळत नाहीत",
	"rc.detail.outline_inside_rim":          "रेषा टायरच्या आतील रिमच्या कडेने जाते",
	"rc.detail.outline_not_outer_edge":      "रेषा टायरची बाहेरील कड नाही",
	"rc.detail.accepted_not_wheel_shaped":   "दृश्य मॉडेलने रेषा स्वीकारली, पण तिचा आकार चाकासारखा नाही",
	"rc.detail.not_the_named_object":        "ज्या वस्तूचे नाव दिले होते ती ही नाही",
	"rc.detail.not_water":                   "हे पाणी नाही",
	"rc.detail.no_mask":                     "रेषा नाही",
}

// The names of the checks every model answer passes (CONTRACTS.md 1.1), as
// a certificate lists them, keyed by name with spaces as underscores.
var enChecks = map[string]string{
	"rc.check.schema":         "schema",
	"rc.check.grid":           "grid",
	"rc.check.inside_image":   "inside the image",
	"rc.check.allowed_values": "allowed values",
}

var taChecks = map[string]string{
	"rc.check.schema":         "பதில் வடிவம்",
	"rc.check.grid":           "கட்டம்",
	"rc.check.inside_image":   "படத்தின் உள்ளே",
	"rc.check.allowed_values": "அனுமதிக்கப்பட்ட மதிப்புகள்",
}

var hiChecks = map[string]string{
	"rc.check.schema":         "जवाब का ढाँचा",
	"rc.check.grid":           "ग्रिड",
	"rc.check.inside_image":   "तस्वीर के अंदर",
	"rc.check.allowed_values": "मान्य मान",
}

var teChecks = map[string]string{
	"rc.check.schema":         "సమాధాన రూపం",
	"rc.check.grid":           "గ్రిడ్",
	"rc.check.inside_image":   "చిత్రం లోపల",
	"rc.check.allowed_values": "అనుమతించిన విలువలు",
}

var knChecks = map[string]string{
	"rc.check.schema":         "ಉತ್ತರದ ರೂಪ",
	"rc.check.grid":           "ಗ್ರಿಡ್",
	"rc.check.inside_image":   "ಚಿತ್ರದ ಒಳಗೆ",
	"rc.check.allowed_values": "ಅನುಮತಿಸಿದ ಮೌಲ್ಯಗಳು",
}

var mlChecks = map[string]string{
	"rc.check.schema":         "മറുപടിയുടെ ഘടന",
	"rc.check.grid":           "ഗ്രിഡ്",
	"rc.check.inside_image":   "ചിത്രത്തിനുള്ളിൽ",
	"rc.check.allowed_values": "അനുവദനീയമായ മൂല്യങ്ങൾ",
}

var bnChecks = map[string]string{
	"rc.check.schema":         "উত্তরের গঠন",
	"rc.check.grid":           "গ্রিড",
	"rc.check.inside_image":   "ছবির ভেতরে",
	"rc.check.allowed_values": "অনুমোদিত মান",
}

var mrChecks = map[string]string{
	"rc.check.schema":         "उत्तराची रचना",
	"rc.check.grid":           "ग्रिड",
	"rc.check.inside_image":   "चित्राच्या आत",
	"rc.check.allowed_values": "परवानगी असलेली मूल्ये",
}
