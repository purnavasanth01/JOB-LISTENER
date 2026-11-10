"""
Filtering rules: what counts as a relevant internship and what gets dropped.

Every decision returns a short human-readable reason so dropped.csv explains itself.
Edit the regex lists below to change what is included / excluded.
"""
import html
import re
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- text helpers

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s):
    """HTML (possibly double-escaped) -> plain text."""
    if not s:
        return ""
    s = html.unescape(html.unescape(str(s)))
    s = re.sub(r"<\s*(br|/p|/li|/div|/h\d)\s*/?>", ". ", s, flags=re.I)
    s = TAG_RE.sub(" ", s)
    return WS_RE.sub(" ", s).strip()


def rx(pattern):
    return re.compile(pattern, re.I)


# ---------------------------------------------------------------- internship?

INTERN_RE = rx(r"\bintern(s|ship|ships)?\b|\bco-?ops?\b|\bsummer (analyst|associate|engineer|program)s?\b"
               r"|\bapprentice(ship)?s?\b|\bstudent (researcher|engineer|developer|programmer)s?\b"
               r"|\bfellowship\b|\bplacement\b|\bindustrial trainee\b")


def is_internship(title):
    return bool(INTERN_RE.search(title or ""))


# ---------------------------------------------------------------- role rules
# HARD exclusions always win (user: no embedded/hardware/robotics/GPU/quant/sales/IT roles).
HARD_EXCLUDE = [
    ("cad/mechanical", rx(r"\bauto ?cad\b|\bcad\b|\bmechanical\b|\bcivil\b|\bstructural\b|\bthermal\b|\bhvac\b"
                          r"|\bmaterials? (engineer|scien)\w*|\bprocess engineer\w*|\bchemical\b")),
    ("embedded/hardware", rx(r"\bembedded\b|\bfirmware\b|\bfpga\b|\basic\b|\brtl\b|\bhardware\b|\bpcb\b|\banalog\b"
                             r"|\brf\b|\bsilicon\b|\bcircuits?\b|\bphysical design\b|\bdft\b|\bdesign verification\b"
                             r"|\b(post-?silicon|silicon|circuit|chip|hardware|soc|pre-?silicon) (validation|verification)\b"
                             r"|\bsemiconductor\b|\belectrical\b|\bpower electronics\b|\boptic(s|al)\b|\bphotonic\w*"
                             r"|\bcustom soc\b|\bmixed[- ]signal\b|\bvlsi\b")),
    ("semiconductor/manufacturing", rx(r"\bdram\b|\bsram\b|\bhbm\b|\bnand\b|\bmemory (device|technology|cell)\b"
                                       r"|\bdevice engineer\w*|\bequipment\b|\byield\b|\bphotomask\b|\bmetrology\b"
                                       r"|\blithograph\w*|\betch\b|\bwafer\w*|\bfab\b|\bprocess (integration|development"
                                       r"|safety)\b|\bpackaging\b|\bsupplier\b|\bcalibration\b|\bate\b|\bdtco\b"
                                       r"|\bpathfinding\b|\bthin film\b|\bplasma\b|\bvacuum\b|\btape-?out\b")),
    ("robotics/autonomy", rx(r"\brobot\w*|\bautonom\w*|\bperception\b|\bself[- ]driving\b|\bmotion planning\b|\bslam\b"
                             r"|\badas\b")),
    ("gpu/perf/architecture", rx(r"\bgpu\b|\bcuda\b|\bperf(ormance)? (engineer\w*|model\w*|analy\w*|architect\w*)\b"
                                 r"|\bcomputer architecture\b|\bcpu\b|\bcompilers?\b|\bhigh[- ]performance computing\b|\bhpc\b")),
    ("quant/trading", rx(r"\bquants?\b|\bquantitative\b|\btraders?\b|\btrading (intern|analyst|associate|desk)\w*"
                         r"|\bsales (and|&) trading\b")),
    ("sales/solutions/support", rx(r"\bsales\b|\bsolutions? (engineer|architect|consultant)\w*|\bpre-?sales\b"
                                   r"|\bcustomer (engineer|success|support)\w*|\bsupport engineer\w*|\btechnical support\b"
                                   r"|\bhelp ?desk\b|\bservice desk\b|\bdesktop support\b|\bit support\b"
                                   r"|\bfield (engineer|service)\w*|\baccount (executive|manager)\b|\bbusiness development\b")),
]

# SOFT exclusions lose to a strong software term ("Software Engineer Intern - Marketing Tech" stays).
SOFT_EXCLUDE = [
    ("it/sysadmin", rx(r"\bsys(tems?)? ?admin\w*|\bnetwork admin\w*|\bit (technician|operations|helpdesk)\b")),
    ("non-tech", rx(r"\bmarketing\b|\baccounting\b|\baudit\w*|\btax\b|\blegal\b|\bparalegal\b|\bhuman resources\b"
                    r"|\bhr\b|\brecruit\w*|\btalent acquisition\b|\bmba\b|\binvestment banking\b|\bwealth\b"
                    r"|\bprivate equity\b|\bcommunications\b|\bjournalis\w*|\bcopywrit\w*|\bgraphic design\w*"
                    r"|\bindustrial\b|\bnurs\w*|\bclinical\b|\bpharm\w*|\bbiolog\w*|\bchemist\w*|\btechnician\b"
                    r"|\bwarehouse\b|\bsupply chain\b|\blogistics\b|\bprocurement\b|\bfacilities\b|\bconstruction\b"
                    r"|\breal estate\b|\bpublic relations\b|\bmanufactur\w*|\bfinance\b|\bfinancial\b|\bactuar\w*"
                    r"|\bnon-?tech\w*|\btechnical writ\w*|\bwriter\b|\bconsult\w*|\bfield\b|\boperations\b"
                    r"|\bstrategy\b|\bsustainab\w*|\besg\b|\benergy\b|\bgis\b|\bgeospatial\b|\bcommercial\b"
                    r"|\bclient\b|\bpolicy\b|\bmilitary\b|\bvehicle design\b|\bdesign studio\b|\becommerce\b"
                    r"|\bscheduling\b|\bestimating\b|\bproject controls\b|\bunderwriting\b")),
    ("non-tech analyst", rx(r"\b(business|financial|finance|investment|credit|risk|operations|ops|pricing|strategy"
                            r"|policy|compliance|actuarial|equity|research|sales|marketing|supply chain|people|hr"
                            r"|program|portfolio|treasury|fp&a) analysts?\b")),
]
STRONG_SOFTWARE = rx(r"\bsoftware\b|\bswe\b|\bsde\b|\bdevelopers?\b|\bfull[- ]?stack\b|\bback[- ]?end\b|\bfront[- ]?end\b"
                     r"|\bmachine learning\b|\bdata (engineer|scien|analy)\w*|\bdevops\b|\bcloud\b|\bai\b|\bml\b"
                     r"|\bsecurity engineer\w*|\bweb\b|\bmobile\b|\bresearch (intern|scien\w*|engineer\w*)\b")

# First matching label wins. Order matters (UX Research must hit UI/UX before Research).
ROLE_RULES = [
    ("Forward Deployed", rx(r"\bforward[- ]deployed\b|\bfde\b")),
    ("UI/UX", rx(r"\bux\b|\bui\b|\buser experience\b|\buser interface\b|\binteraction design\w*|\bproduct design\w*"
                 r"|\bdesign technologist\b|\bweb design\w*|\bux research\w*|\bui/ux\b")),
    ("Security", rx(r"\bsecurity\b|\bcyber\w*|\bappsec\b|\binfosec\b|\bpenetration\b|\bthreat\b|\bvulnerabilit\w*"
                    r"|\bidentity (and|&) access\b|\bsoc analyst\b")),
    ("Product/Program", rx(r"\bproduct manag\w*|\bassociate product manager\b|\bapm\b|\bprogram manag\w*|\btpm\b"
                           r"|\btechnical program\b|\bproduct owner\b|\bproduct (intern|management)\b")),
    ("Research", rx(r"\bresearch (intern|scien\w*|engineer\w*|fellow\w*|resident\w*)\b|\bapplied scien\w*"
                    r"|\bstudent researcher\b|\bmember of technical staff\b")),
    ("AI/ML", rx(r"\bmachine learning\b|\bml\b|\bmlops\b|\bai\b|\bartificial intelligence\b|\bdeep learning\b|\bnlp\b"
                 r"|\bllms?\b|\bgen ?ai\b|\bgenerative\b|\bcomputer vision\b|\bai/ml\b|\bml/ai\b|\bagi\b"
                 r"|\bfoundation(al)? models?\b|\b(representation|reinforcement) learning\b")),
    ("Data", rx(r"\bdata\b|\banalytics\b|\bbusiness intelligence\b|\bbi\b|\bstatistic\w*|\betl\b|\binformatics\b"
                r"|\bproduct analyst\b")),
    ("Cloud/DevOps", rx(r"\bcloud\b|\bdevops\b|\bdev ops\b|\bsre\b|\bsite reliability\b|\binfrastructure\b|\bplatform\b"
                        r"|\bkubernetes\b|\bdistributed systems\b|\bnetwork engineer\w*|\bsystems? engineer\w*"
                        r"|\bproduction engineer\w*|\bdatabase\b")),
    ("QA/Automation", rx(r"\bqa\b|\bquality (assurance|engineer\w*)\b|\btest\w*|\bsdet\b|\bautomation\b|\bvalidation\b")),
    ("Tech Analyst", rx(r"\b(technology|tech|engineering|it|digital|software|systems|information) (summer )?analysts?\b"
                        r"|\btechnology (summer )?(intern|program)\w*")),
    ("Software", rx(r"\bsoftware\b|\bswe\b|\bsde\b|\bdevelopers?\b|\b(software|application|web|app) development\b|\bprogramm(er|ing)\b|\bfull[- ]?stack\b"
                    r"|\bback[- ]?end\b|\bfront[- ]?end\b|\bweb\b|\bmobile\b|\bios\b|\bandroid\b|\bapplications?\b"
                    r"|\bapis?\b|\bcoding\b|\bcomputer science\b|\bcs\b|\bengineering (intern|co-?op)\w*|\bdigital\b"
                    r"|\blibrar(y|ies)\b|\balgorithms?\b|\bsdks?\b|\bframeworks?\b|\btooling\b|\bnumerical\b"
                    r"|\bscientific computing\b|\bsolvers?\b|\bcompilers?\b")),
]
# user: include robotics / embedded software and compilers, but not pure robotics / embedded / hardware engineering
SOFTWARE_OVERRIDES = {"embedded/hardware", "robotics/autonomy", "gpu/perf/architecture"}
SOFTWARE_WORD = rx(r"\bsoftware\b|\bcompilers?\b|\bswe\b|\bsde\b")
GENERIC_TECH = rx(r"\bengineer\w*|\btechnolog\w*|\btechnical\b|\btech\b|\bcomputer\b|\bstem\b|\binformation systems\b")
ANALYST_RE = rx(r"\banalysts?\b")
ANALYST_OK_LABELS = {"Data", "Tech Analyst", "Security", "AI/ML", "Research"}


def classify_role(title, category=None):
    """-> (label, None) if relevant, else (None, reason)."""
    t = title or ""
    for name, pat in HARD_EXCLUDE:
        if pat.search(t):
            if name in SOFTWARE_OVERRIDES and SOFTWARE_WORD.search(t):
                continue      # "Embedded Software Engineer", "Robotics Software", "GPU Compiler" stay in
            return None, "role excluded: " + name
    strong = bool(STRONG_SOFTWARE.search(t))
    if not strong:
        for name, pat in SOFT_EXCLUDE:
            if pat.search(t):
                return None, "role excluded: " + name
    label = None
    for name, pat in ROLE_RULES:
        if pat.search(t):
            label = name
            break
    if label and ANALYST_RE.search(t) and label not in ANALYST_OK_LABELS:
        return None, "role excluded: non-tech analyst"
    if label:
        return label, None
    # aggregator category only decides for completely generic titles ("Summer 2027 Intern")
    if category and re.search(r"software|ai/ml|data", category, re.I) and not title_tokens(t):
        return "General (check)", None
    if GENERIC_TECH.search(t):
        return "General (check)", None
    return None, "not a software role"


def category_excluded(category, title):
    """Simplify tags rows Hardware/Quant; trust that unless the title is clearly software."""
    if category and re.search(r"hardware|quant", category, re.I) and not STRONG_SOFTWARE.search(title or ""):
        return "role excluded: category " + category
    return None


# ---------------------------------------------------------------- term (Spring/Summer/Fall 2027 or none)

SEASON_YEAR_RE = rx(r"\b(spring|summer|fall|autumn|winter)\s*[-/,']?\s*(?:20)?(2[3-9])\b")
TITLE_YEAR_RE = re.compile(r"\b20(2[3-9])\b")
DESC_TERM_RE = rx(r"(?:intern\w*|co-?op|program|term|session|cohort)\W+(?:\w+\W+){0,4}?"
                  r"(?:spring|summer|fall|autumn|winter)\s*(?:20)?(2[3-9])\b"
                  r"|(?:spring|summer|fall|autumn|winter)\s*(?:20)?(2[3-9])\W+(?:\w+\W+){0,2}?"
                  r"(?:intern\w*|co-?op|program|term|session|cohort)")


def term_check(title, terms=None, season=None, desc=None):
    """-> None if OK, else reason. Accepts any 2027 term or no term at all."""
    if terms:
        joined = " ".join(terms)
        if "2027" in joined or re.search(r"n/?a", joined, re.I):
            return None
        return "term: " + ", ".join(terms)
    text = "%s %s" % (title or "", season or "")
    years = {m.group(2) for m in SEASON_YEAR_RE.finditer(text)} | set(TITLE_YEAR_RE.findall(text))
    if years:
        return None if "27" in years else "term: 20" + "/20".join(sorted(years))
    if desc:
        if "2027" in desc:
            return None
        years = {a or b for a, b in DESC_TERM_RE.findall(desc)}
        if years and "27" not in years:
            return "term (from description): 20" + "/20".join(sorted(years))
    return None


# ---------------------------------------------------------------- location (US only)

US_STATE_CODES = set("""AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ
NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR""".split())
US_STATE_NAMES = rx(r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia"
                    r"|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts"
                    r"|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey"
                    r"|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania"
                    r"|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington"
                    r"|west virginia|wisconsin|wyoming|district of columbia)\b")
US_MARKER = rx(r"united states|\busa\b|\bu\.s\.a?\.?(?=\W|$)|^\s*us\b|\bus\s*[-,(]|\bus\s*$")
US_CITIES = rx(r"\b(new york|nyc|san francisco|seattle|austin|boston|chicago|los angeles|mountain view|sunnyvale"
               r"|palo alto|menlo park|redmond|bellevue|san jose|santa clara|cupertino|atlanta|dallas|houston"
               r"|denver|pittsburgh|philadelphia|miami|phoenix|san diego|portland|raleigh|durham|salt lake"
               r"|minneapolis|detroit|nashville|charlotte|columbus|irvine|plano|jersey city|hoboken|reston"
               r"|mclean|tampa|orlando|st\.? louis|kansas city|bentonville|round rock|college station|san antonio"
               r"|fort worth|irving|richardson|frisco|santa monica|bay area|silicon valley|ann arbor|madison"
               r"|baltimore|herndon|chantilly|cambridge, ma|burlington|westlake|o'?fallon|wilmington|newark"
               r"|princeton|stamford|sacramento|oakland|berkeley|boulder|lehi|provo|scottsdale|tempe|chandler"
               r"|cincinnati|cleveland|indianapolis|milwaukee|omaha|des moines|louisville|memphis|richmond"
               r"|santa barbara|hillsboro|folsom|san mateo|redwood city|foster city|los gatos|milpitas)\b")
NON_US = rx(r"\b(canada|india|china|united kingdom|uk|england|scotland|germany|france|ireland|mexico|brazil|japan"
            r"|singapore|israel|poland|netherlands|switzerland|australia|korea|taiwan|hong kong|spain|italy"
            r"|sweden|denmark|norway|finland|romania|czech\w*|hungary|portugal|vietnam|philippines|malaysia"
            r"|indonesia|thailand|uae|dubai|saudi|egypt|nigeria|kenya|south africa|argentina|chile|colombia"
            r"|peru|costa rica|belgium|austria|greece|turkey|ukraine|serbia|bulgaria|estonia|lithuania|latvia"
            r"|new zealand|pakistan|bangladesh|sri lanka|luxembourg|toronto|vancouver|montreal|ottawa|waterloo"
            r"|calgary|edmonton|ontario|quebec|british columbia|alberta|bangalore|bengaluru|hyderabad|pune"
            r"|chennai|gurgaon|gurugram|noida|mumbai|delhi|shanghai|beijing|shenzhen|hangzhou|tokyo|seoul"
            r"|sydney|melbourne|paris|berlin|munich|amsterdam|dublin|tel aviv|warsaw|krakow|bucharest|cluj"
            r"|prague|lisbon|madrid|barcelona|milan|stockholm|copenhagen|oslo|helsinki|hsinchu|taipei|manila"
            r"|kuala lumpur|jakarta|bangkok|cairo|lagos|nairobi|cape town|s[aã]o paulo|buenos aires|santiago"
            r"|bogota|lima|zurich|zürich|london|cambridge, uk|emea|apac|latam|qatar|doha|abu dhabi|riyadh|kuwait"
            r"|bahrain|oman|jordan|lebanon|morocco|tunisia|ghana|ethiopia|rwanda|uganda|tanzania|kazakhstan"
            r"|uzbekistan|nepal|cambodia|vietnam|hanoi|ho chi minh|united arab emirates|cyprus|malta|iceland"
            r"|croatia|slovakia|slovenia|armenia|georgia, tbilisi|tbilisi|belgrade|sofia|kyiv|lviv|tallinn"
            r"|vilnius|riga|guadalajara|monterrey|mexico city|bangkok|singapore|auckland|wellington)\b")


def _loc_kind(loc):
    """'us' | 'non-us' | 'unknown' for one location string."""
    l = (loc or "").strip()
    if not l:
        return "unknown"
    if US_MARKER.search(l):
        return "us"
    non_us = bool(NON_US.search(l))
    tokens = re.split(r"[,\-/|()]+", l)
    codes = [t.strip() for t in tokens if len(t.strip()) == 2]
    if not non_us and any(c in US_STATE_CODES for c in codes):
        return "us"
    if not non_us and (US_STATE_NAMES.search(l) or US_CITIES.search(l)):
        return "us"
    if non_us:
        return "non-us"
    return "unknown"


def location_check(locations):
    """-> None if keep (any US or unknown), else reason (all locations clearly non-US)."""
    if isinstance(locations, str):
        locations = [locations]
    locs = []
    for l in locations or []:   # "Bellevue, WA; Toronto, Canada" is two locations, not one
        locs += [p for p in re.split(r"\s*(?:;|\||\n| / )\s*", str(l or "")) if p.strip()]
    if not locs:
        return None
    kinds = [_loc_kind(str(l)) for l in locs]
    if "us" in kinds or "unknown" in kinds:
        return None
    return "location not US: " + "; ".join(str(l) for l in locs)[:80]


# ---------------------------------------------------------------- eligibility (user rules)
#
# DROP if the description says any of:
#   citizen / green card / asylee / clearance / US-persons only, ITAR, F-1/CPT/OPT not eligible,
#   no sponsorship "now or in the future" / no H-1B, undergrad-only, PhD-only.
# Otherwise the alert carries the posting's own sponsorship sentence so the user can decide.

CITIZEN = rx(r"\b(u\.?\s?s\.?|united states|american)\s+citizens?(hip)?\b|\bcitizenship\b|\bcitizens? only\b")
PERM_RES = rx(r"\bpermanent residen\w*|\bgreen card\b|\blawful(ly)? permanent\b")
ASYLEE = rx(r"\basylees?\b|\brefugees?\b|\bprotected individuals?\b")
US_PERSON = rx(r"\bu\.?\s?s\.?\s+persons?\b")
CLEARANCE = rx(r"\b(security|secret|government|federal|dod|public trust|ts/sci|top secret|dhs|doe|q)\s+clearances?\b"
               r"|\bclearances?\s+(is\s+|are\s+)?required\b|\bactive clearance\b|\bts/sci\b|\btop secret\b"
               r"|\bclearable\b|\bobtain (and maintain )?(a |an )?clearance\b")
REQ = rx(r"\b(must|required|requires|requirement|require|only|eligib\w*|need to|needs to|mandatory|limited to"
         r"|restricted to|ability to obtain|able to obtain|obtain and maintain|possess|hold|necessary)\b")
NEG = rx(r"\b(not required|no (security )?clearance|does not require|doesn'?t require|not need|not necessary"
         r"|regardless of|without regard|irrespective of|equal opportunity|discriminat\w*|protected (veteran|class)"
         r"|eeo|affirmative action|e-?verify|unless)\b")
# a sentence that also talks about visas/work authorization is an inclusive list, not a citizen-only rule
INCLUSIVE = rx(r"\bwork authori[sz]\w*|\bauthori[sz]ation to work\b|\bcpt\b|\bopt\b|\bf-?1\b|\bvisa\b|\bh-?1b\b"
               r"|\bauthori[sz]ed to work\b")
COND = rx(r"\b(may|might|could|if|depending|potentially|some positions|certain positions|in some cases)\b")
SENT_SPLIT = re.compile(r"(?<=[.!?;•])\s+|\n+")
CITIZEN_LABEL = rx(r"^((citizenship|clearance)( requirements?| level)?\s*[:\-]\s*)?(us|united states)\s+citizen(ship|s)?"
                   r"( required| only)?\s*\.?$")

ITAR = rx(r"\bitar\b|international traffic in arms")
SPONSOR_WORD = rx(r"sponsor\w*|\bvisa\b|authori[sz]\w*|immigration")
_NOT = r"\b(not|unable|cannot|can'?t|won'?t|no|without)\b"
FUTURE_SPONSOR = rx(r"now or in the future|currently or in the future|now or at any (point|time) in the future"
                    r"|" + _NOT + r"[^.]{0,60}\bsponsor\w*[^.]{0,60}\b(future|later date)\b"
                    r"|" + _NOT + r"[^.]{0,60}\bfuture\b[^.]{0,40}\bsponsor")
H1B_NO = rx(r"\b(not|unable to|cannot|can'?t|won'?t|will not|does not|do not|no)\s+(\w+\s+){0,5}h-?1-?b\b"
            r"|\bh-?1-?b\b[^.]{0,50}\b(not available|not offered|not provided|not supported|unavailable|not eligible)\b")
F1_EXCLUDED = rx(r"\bf-?1\b[^.]{0,140}\b(not eligible|ineligible|unable|cannot|will not|won'?t)\b"
                 r"|\b(cpt|opt)\b[^.]{0,90}\b(not eligible|will not|won'?t|cannot|unable)\b"
                 r"|\b(not eligible|ineligible)\b[^.]{0,90}\bf-?1\b")
F1_WELCOME = rx(r"\bf-?1\b[^.]{0,90}\beligible\b|\bcpt\b[^.]{0,60}\b(eligible|accepted|welcome|supported)\b")
EAR = rx(r"\bexport control\w*|\bear\b|\bexport[- ]controlled\b")

PHD_TITLE = rx(r"\bph\.?\s?d\.?\b|\bdoctora\w*")
NON_PHD_DEGREE = rx(r"\b(bs|ba|ms|m\.s|msc|master\w*|bachelor\w*|undergrad\w*|mba|b\.s|bs/ms|ms/phd|bsms|bs-ms)\b")
PHD_DESC = rx(r"(currently|actively)\s+(enrolled|pursuing)\s+(in\s+)?(a\s+)?ph\.?\s?d|ph\.?\s?d\.?\s+(students?|candidates?)"
              r"\s+only|must be (a\s+|an\s+)?(current\s+)?ph\.?\s?d|enrolled in a doctoral")
NON_PHD_DESC = rx(r"\bmaster'?s\b|\bm\.?s\.?\b|\bbachelor'?s\b|\bundergrad\w*|\bbs/ms\b|\bms/phd\b")
# a single requirement sentence demanding a PhD (catches "Master's" titles whose body wants a PhD)
PHD_REQ_SENT = rx(r"\b(must|required|requires|only)\b[^.]{0,60}\b(ph\.?\s?d|doctoral)\b"
                  r"|\b(ph\.?\s?d|doctoral)\b[^.]{0,40}\b(required|only)\b")

UNDERGRAD_TITLE = rx(r"\b(freshm[ae]n|sophomores?|rising (senior|junior|sophomore)s?|high school"
                     r"|bachelor'?s only|undergrad(uate)?s? only)\b")
UNDERGRAD_WORD = rx(r"\bundergrad\w*")
GRAD_OK = rx(r"\bgraduate\b|\bmaster\w*|\bms\b|\bm\.s\b|\bmba\b|\bph\.?\s?d\b|\bbs/ms\b")
UNDERGRAD_DESC = rx(r"(pursuing|enrolled in|working toward?s?)\s+(a |an |your )?(bachelor'?s|undergraduate|b\.?s\.?)"
                    r"(\s+degree)?(?!\s*(or|/|and)\s*(a\s+)?(master|graduate|m\.?s|advanced|ph))"
                    r"|\brising (senior|junior|sophomore)s?\b|\bmust be an? undergraduate\b|\bundergraduate students? only\b"
                    r"|\b(freshman|sophomore|junior) (standing|year)\b")
NON_UNDERGRAD_DESC = rx(r"\bmaster'?s\b|\bm\.?s\.?\b(?!\s*(office|excel|word|teams|365))|\bgraduate (degree|students?|program"
                        r"|level|school)\b|\badvanced degree\b|\bph\.?\s?d\b|\bmba\b|\bpost-?graduate\b|\bbs/ms\b"
                        # "Bachelor's degree or above / and higher / or beyond" is open to Master's students
                        r"|\b(bachelor'?s?|undergrad\w*|b\.?s\.?)\b(\s+degree)?[^.;\n]{0,50}?\b(or|and|and/or|/)\s+(above|higher|beyond|more advanced"
                        r"|up to (a )?(master|phd|doctor))\b")

MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"])}
SEASON_MONTH = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12}
GRAD_SENT = rx(r"\bgraduat\w*|\bclass of 20\d\d")
DATE_RE = rx(r"\b(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?|(spring|summer|fall|autumn|winter))?"
             r"[\s,/]*(20[2-3]\d)\b")
USER_GRAD = 2028 * 12 + 5    # May 2028

SPONS_LINE = rx(r"sponsor\w*|\bvisas?\b|work authori[sz]\w*|authori[sz]ed to work|authori[sz]ation to work"
                r"|\bimmigration\b|\bh-?1-?b\b|\bcpt\b|\bopt\b|\bf-?1\b")
SPONS_NOISE = rx(r"equal opportunity|e-?verify|regardless of|discriminat|citizenship status|protected veteran"
                 r"|company h-?1-?b sponsorship|h-?1-?b sponsorship (history|data|stats)")

TECH_WORDS = re.compile(r"(?<![\w+#])(python|java|javascript|typescript|c\+\+|c#|golang|rust|scala|kotlin|swift|"
                        r"sql|nosql|postgres\w*|mysql|mongodb|apis?|rest(?:ful)?|graphql|microservices?|cloud|aws|azure|"
                        r"gcp|docker|kubernetes|react|angular|vue|node\.?js|linux|git|ci/cd|machine learning|"
                        r"deep learning|tensorflow|pytorch|software|algorithms?|data structures|back-?end|front-?end|"
                        r"full[- ]stack|devops|databases?|spark|hadoop|llms?|nlp|computer science|programming|"
                        r"coding|object[- ]oriented)(?![\w+#])", re.I)


def _sentences(desc):
    # "U.S." / "e.g." contain periods that would split sentences in the wrong place
    desc = re.sub(r"\bU\.\s?S\.(\s?A\.)?", "US", desc, flags=re.I)
    desc = re.sub(r"\b(e\.g|i\.e|etc|incl|approx)\.", r"\1", desc, flags=re.I)
    return [s.strip()[:600] for s in SENT_SPLIT.split(desc) if s.strip()]


def tech_word_count(desc):
    words = set()
    for m in TECH_WORDS.finditer(desc or ""):
        w = m.group(1).lower().replace("-", "").replace(" ", "")
        words.add(w[:-1] if w.endswith("s") and w[:-1] in ("api", "algorithm", "database", "microservice", "llm")
                  else w)
    return len(words)


def phd_only(title, desc=None, degrees=None):
    t = title or ""
    if PHD_TITLE.search(t) and not NON_PHD_DEGREE.search(t):
        return True
    if degrees and all(re.search(r"phd|doctor", d, re.I) for d in degrees):
        return True
    if desc and PHD_DESC.search(desc) and not NON_PHD_DESC.search(desc):
        return True
    return False


def undergrad_only_title(title):
    t = title or ""
    if UNDERGRAD_TITLE.search(t):
        return True
    return bool(UNDERGRAD_WORD.search(t) and not GRAD_OK.search(t))


def _grad_dates(s):
    out = []
    for m in DATE_RE.finditer(s):
        mon = MONTHS.get((m.group(1) or "")[:3].lower()) or SEASON_MONTH.get((m.group(2) or "").lower())
        yr = int(m.group(3))
        out.append((yr * 12 + (mon or 1), yr * 12 + (mon or 12)))
    return out


def grad_window_issue(desc):
    """Sentence whose graduation window clearly excludes May 2028, else None (flag only, never a drop)."""
    for s in _sentences(desc or ""):
        if not GRAD_SENT.search(s):
            continue
        dates = _grad_dates(s)
        if not dates:
            continue
        lo, hi = min(d[0] for d in dates), max(d[1] for d in dates)
        if len(dates) >= 2:
            ok = lo - 2 <= USER_GRAD <= hi + 2
        elif re.search(r"\bby\b|\bbefore\b|no later than|prior to", s, re.I):
            ok = USER_GRAD <= hi + 2
        elif re.search(r"or later|\bafter\b|or after|\bbeyond\b", s, re.I):
            ok = USER_GRAD >= lo - 2
        else:
            ok = lo - 3 <= USER_GRAD <= hi + 3
        if not ok:
            return s[:160]
    return None


def sponsorship_line(desc):
    """The posting's most relevant sponsorship / work-authorization sentence."""
    best, score = None, 0
    for s in _sentences(desc or ""):
        if not SPONS_LINE.search(s) or SPONS_NOISE.search(s) or len(s.split()) < 5:
            continue       # very short fragments are page widgets ("H1B Sponsorship"), not the posting's words
        sc = 3 if re.search(r"sponsor", s, re.I) else 2 if re.search(r"authori[sz]", s, re.I) else 1
        if sc > score:
            best, score = s, sc
    if best and len(best) > 220:
        best = best[:217].rsplit(" ", 1)[0] + "…"
    return best


def check_eligibility(title, desc=None, degrees=None, sponsorship=None):
    """-> {"drop": reason|None, "evidence": sentence, "line": sponsorship sentence|None, "flags": [..]}"""
    res = {"drop": None, "evidence": "", "line": None, "flags": []}

    def drop(reason, evidence=""):
        res["drop"], res["evidence"] = reason, evidence[:240]
        return res

    if sponsorship and re.search(r"citizenship is required", sponsorship, re.I):
        return drop("requires US citizenship (list flag)", sponsorship)
    if phd_only(title, desc, degrees):
        return drop("PhD only", title)
    if undergrad_only_title(title):
        return drop("undergrad only", title)
    if not desc:
        if sponsorship and re.search(r"does not offer", sponsorship, re.I):
            res["line"] = "Listed as: Does Not Offer Sponsorship"
        return res
    for s in _sentences(desc):
        if ITAR.search(s):
            return drop("ITAR (US persons only)", s)
        if CITIZEN_LABEL.match(s):          # requirement field like "Citizenship: US Citizen"
            return drop("requires US citizenship / PR / asylee", s)
        hit = None
        if CLEARANCE.search(s):
            hit = "requires security clearance"
        elif CITIZEN.search(s) or US_PERSON.search(s) or PERM_RES.search(s) or ASYLEE.search(s):
            hit = "requires US citizenship / PR / asylee"
        if hit and not (NEG.search(s) or INCLUSIVE.search(s) or COND.search(s)) and REQ.search(s):
            return drop(hit, s)
        if F1_EXCLUDED.search(s):
            return drop("F-1 / CPT / OPT not eligible", s)
        if SPONSOR_WORD.search(s) and FUTURE_SPONSOR.search(s):
            return drop("no sponsorship now or in the future", s)
        if H1B_NO.search(s):
            return drop("no H-1B sponsorship", s)
        if PHD_REQ_SENT.search(s) and not NON_PHD_DESC.search(s):
            return drop("PhD required (description)", s)
    if UNDERGRAD_DESC.search(desc) and not NON_UNDERGRAD_DESC.search(desc):
        m = UNDERGRAD_DESC.search(desc)
        return drop("undergrad only (description)", desc[max(0, m.start() - 60):m.end() + 60])
    res["line"] = sponsorship_line(desc)
    if not res["line"] and sponsorship and re.search(r"does not offer", sponsorship, re.I):
        res["line"] = "Listed as: Does Not Offer Sponsorship"
    g = grad_window_issue(desc)
    if g:
        res["flags"].append("🎓 check graduation date: \"%s\"" % g)
    if F1_WELCOME.search(desc):
        res["flags"].append("✓ F-1/CPT welcome")
    if EAR.search(desc):
        res["flags"].append("⚠ export-control mentioned")
    return res


def hard_drop(title, desc=None, degrees=None, sponsorship=None):
    """Reason the job must be ignored, else None (thin wrapper used by list-level checks and tests)."""
    return check_eligibility(title, desc, degrees, sponsorship)["drop"]


# ---------------------------------------------------------------- identity / de-duplication

COMPANY_STOP = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "plc", "lp", "group",
                "holdings", "technologies", "technology", "usa", "us", "na", "incorporated"}


def norm_company(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return "".join(w for w in s.split() if w not in COMPANY_STOP)


TITLE_STOP = {"intern", "interns", "internship", "internships", "co", "op", "coop", "summer", "fall", "spring",
              "winter", "autumn", "the", "a", "an", "and", "of", "for", "in", "at", "to", "with", "program", "us",
              "usa", "united", "states", "remote", "hybrid", "onsite", "on", "site", "new", "grad", "student",
              "students", "position", "role", "opportunity", "early", "career", "careers", "university", "campus",
              "undergraduate", "graduate", "bs", "ms", "masters", "master", "bachelors", "s", "m", "b", "ii",
              "iii", "i", "level", "entry", "or", "x", "amp"}


def title_tokens(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return frozenset(w for w in words if w not in TITLE_STOP and not re.fullmatch(r"20\d\d|\d{1,2}", w))


def similar_titles(a, b):
    """Strict on purpose: a false 'duplicate' would hide a real job."""
    if not a or not b:
        return False
    inter = len(a & b)
    if inter / len(a | b) >= 0.8:
        return True
    return inter >= 3 and inter == min(len(a), len(b))


def workday_req_id(path):
    seg = (path or "").rstrip("/").split("/")[-1]
    if "_" not in seg:
        return None
    rid = seg.rsplit("_", 1)[1]
    return re.sub(r"(?<=[A-Za-z0-9])-\d{1,2}$", "", rid) or None


def url_keys(url):
    """Canonical keys for a job URL, so the same job found via different sources/links matches."""
    keys = set()
    if not url:
        return keys
    try:
        p = urlparse(url.strip())
    except ValueError:
        return keys
    host = p.netloc.lower().split(":")[0]
    path = p.path.rstrip("/")
    q = parse_qs(p.query)
    for k in ("gh_jid", "token"):
        if k in q and q[k][0].isdigit() and ("greenhouse" in host or k == "gh_jid"):
            keys.add("gh:" + q[k][0])
    m = re.search(r"/jobs/(\d{5,})", path)
    if m and "greenhouse" in host:
        keys.add("gh:" + m.group(1))
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        rid = workday_req_id(path)
        if rid:
            if "myworkdaysite.com" in host:
                m = re.search(r"/recruiting/([^/]+)/", path)
                tenant = m.group(1) if m else host
            else:
                tenant = host.split(".")[0]
            keys.add("wd:%s:%s" % (tenant.lower(), rid.lower()))
    m = re.search(r"jobs\.lever\.co/[^/]+/([0-9a-f-]{36})", url)
    if m:
        keys.add("lever:" + m.group(1))
    m = re.search(r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})", url)
    if m:
        keys.add("ashby:" + m.group(1))
    m = re.search(r"smartrecruiters\.com/[^/]+/(\d{9,})", url)
    if m:
        keys.add("sr:" + m.group(1))
    m = re.search(r"amazon\.jobs/(?:[a-z-]+/)?jobs/(\d+)", url)
    if m:
        keys.add("amazon:" + m.group(1))
    m = re.search(r"oraclecloud\.com/.*/job/(\d+)", url)
    if m:
        keys.add("orc:%s:%s" % (host, m.group(1)))
    m = re.search(r"careers/applications/jobs/results/(\d{6,})", url)
    if m:
        keys.add("google:" + m.group(1))
    m = re.search(r"(?:microsoft\.com).*/job/(\d+)", url)
    if m:
        keys.add("ms:" + m.group(1))
    m = re.search(r"netflix\.net/careers/job/(\d+)", url)
    if m:
        keys.add("nfx:" + m.group(1))
    m = re.search(r"/careers-home/jobs/(\d+)", url)
    if m:
        keys.add("phenom:%s:%s" % (host, m.group(1)))
    clean_path = re.sub(r"/(en|en-us|en_us|en-gb)(?=/)", "", path.lower())
    keys.add("u:" + host.replace("www.", "") + clean_path)
    return keys
