#!/usr/bin/env python3
"""Build data/crosswalk/revenue_sources.csv and revenue_aliases.csv.

The rules below map every printed (book, fund_group, fund_name, category, line_name)
in data/extracted/*/revenues.csv to a canonical rev_id. The alias file written here is
the source of truth; scripts/check_crosswalk_revenue.py verifies it independently.

Run:  .venv/bin/python scripts/build_crosswalk_revenue.py
"""
import csv
import glob
import os
import re
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "crosswalk")

GF_CATS = ["Property taxes", "Sales taxes", "Other taxes", "Licenses and permits",
           "Intergovernmental", "Service charges", "Fines and forfeits",
           "Miscellaneous", "Other financing sources"]

# rev_id -> (name, category, plain_description)
SOURCES = {}


def src(rev_id, name, cat, desc):
    SOURCES[rev_id] = (name, cat, desc)


# ---- Property taxes -------------------------------------------------------------
src("property-tax-general", "Property tax, general operating", "Property taxes",
    "Tax on the assessed value of real estate and business personal property that pays for general city services (current-year collections, plus small special assessments).")
src("property-tax-police-fire-dedicated", "Property tax, dedicated to police and fire", "Property taxes",
    "Property tax millages voted by residents that can only be spent on the police and fire departments (shown as separate millage lines from 2025).")
src("property-tax-nord-dedicated", "Property tax, dedicated to recreation (NORD/NORDC)", "Property taxes",
    "A 1.5-mill property tax set aside for the city's recreation department (NORD, now NORDC).")
src("property-tax-parkway-dedicated", "Property tax, dedicated to parks and parkways", "Property taxes",
    "A 1.5-mill property tax set aside for the Parks and Parkways department.")
src("property-tax-streets-dedicated", "Property tax, dedicated to streets", "Property taxes",
    "A 1.9-mill property tax set aside for street maintenance; budgeted at about $7 million a year until about 2019 and almost nothing since.")
src("property-tax-prior-year", "Property tax, back taxes from prior years", "Property taxes",
    "Property tax collected late, for earlier tax years, on the general (non-dedicated) rolls.")
src("property-tax-interest-penalties", "Property tax interest and penalties", "Property taxes",
    "Interest and penalties charged on delinquent property tax bills.")

# ---- Sales taxes ----------------------------------------------------------------
src("sales-tax-general", "Sales and use tax (city share)", "Sales taxes",
    "The city's share of the local sales tax on goods and services, including (from 2025) food and drug sales and the tax on out-of-state online purchases. CAUTION: FY2004-2007 (all books) and FY2008-2013 in books through 2013 print hotel/motel and motor vehicle tax inside this line; use the 'sales-and-related-taxes' rollup for a series that is comparable across all years.")
src("sales-tax-motor-vehicle", "Motor vehicle sales tax", "Sales taxes",
    "The city's share of the sales tax on vehicle purchases, printed as 'Motor Vehicle Tax'.")

# ---- Other taxes ----------------------------------------------------------------
src("hotel-motel-tax", "Hotel and motel tax", "Other taxes",
    "Tax paid by visitors on hotel and motel room stays in New Orleans.")
src("utility-tax", "Utility tax", "Other taxes",
    "Tax on electricity and gas bills of customers in the city (Entergy New Orleans).")
src("parking-tax", "Parking tax", "Other taxes",
    "Tax on parking charged at garages and lots in the city.")
src("document-transaction-tax", "Document transaction tax", "Other taxes",
    "Tax paid when property deeds, mortgages and similar documents are recorded.")
src("other-taxes-misc", "Other small taxes", "Other taxes",
    "Smaller taxes lumped together: beer, wine and liquor, chain stores, racing and slot machines, airport, sports wagering, Sewerage and Water Board tax, and tax-collection costs.")

# ---- Licenses and permits -------------------------------------------------------
src("franchise-fees-utilities", "Franchise fees (electric, cable, telephone, wireless)", "Licenses and permits",
    "What utilities such as Entergy New Orleans, cable and telephone companies pay the city for the right to use public streets and rights-of-way.")
src("building-permits", "Building permits", "Licenses and permits",
    "Fees for building permits, demolition permits and use-and-occupancy certificates.")
src("occupational-licenses", "Occupational and business licenses", "Licenses and permits",
    "Annual licenses that businesses and professionals must buy to operate in the city, including the insurance premium license tax.")
src("alcoholic-beverage-permits", "Alcoholic beverage permits", "Licenses and permits",
    "Fees for permits to sell alcohol in bars, restaurants and stores.")
src("taxi-tour-rideshare-licenses", "Taxi, tour guide and ride-hail licenses", "Licenses and permits",
    "Permits and fees for taxis, tour guides and operators, and ride-hail (Uber/Lyft-type) companies.")
src("short-term-rental-permits", "Short-term rental permits", "Licenses and permits",
    "Permit fees on Airbnb-style short-term rentals; only printed separately from the 2025 books (earlier years sit inside 'licenses-permits-other').")
src("motor-vehicle-permits", "Motor vehicle permits and brake tags", "Licenses and permits",
    "Fees tied to vehicles, mainly the brake-tag inspection stickers.")
src("safety-electrical-mechanical-permits", "Electrical, mechanical and safety permits", "Licenses and permits",
    "Permits and licences for electrical, mechanical, gas and similar trade work and inspections.")
src("licenses-permits-other", "Other licenses and permits", "Licenses and permits",
    "All other permits: street cuts and closures, mayoralty permits (parades, film), signs, historic-district reviews and other small fees.")

# ---- Intergovernmental ----------------------------------------------------------
src("harrahs-casino-rent", "Harrah's casino rent", "Intergovernmental",
    "Rent from Harrah's casino on Canal Street; printed as 'Harrah's Rent' under Intergovernmental from the 2021 book, which moved it there from Service Charges (earlier years sit inside 'general-government-charges').")
src("state-revenue-sharing", "State revenue sharing", "Intergovernmental",
    "Money the state shares with local governments out of state tax revenue, including amounts dedicated to police and fire.")
src("state-aid-video-poker", "State video poker revenue", "Intergovernmental",
    "The city's share of state-licensed video poker proceeds.")
src("state-aid-medicaid", "State Medicaid payments", "Intergovernmental",
    "State payments to the city for Medicaid ambulance services (Upper Payment Limit program).")
src("parish-transportation-fund", "Parish transportation fund (roads and transit)", "Intergovernmental",
    "State gasoline-tax money returned to the parish for roads and for transit.")
src("state-supplemental-pay", "State supplemental pay for police and fire", "Intergovernmental",
    "State payments toward extra pay for police officers and firefighters ($10-14 million a year in 2009-2013); printed as 'State Aid Other' through the 2020 book and as 'State Supplemental Pay' from the 2026 books.")
src("intergovernmental-other", "Other state and federal aid", "Intergovernmental",
    "Smaller aid lines: other state aid, corrections, federal aid, emergency management, GOMESA oil revenue, the Orleans Parish Communication District and the like.")

# ---- Service charges ------------------------------------------------------------
src("sanitation-service-charge", "Sanitation service charge", "Service charges",
    "The garbage collection and curbside recycling fee charged to residents and businesses.")
src("parking-meters", "Parking meters", "Service charges",
    "Coins and app payments collected at the city's parking meters, including temporary meters.")
src("emergency-medical-fees", "Emergency medical (ambulance) fees", "Service charges",
    "Fees billed to patients and insurers for EMS ambulance transport; printed separately only from 2025 (earlier books hold it inside 'public-safety-charges').")
src("public-safety-charges", "Public safety charges", "Service charges",
    "Fees for public safety services, e.g. airport fire protection and police reports; in books before 2025 this single line also contains ambulance fees.")
src("general-government-charges", "General government charges", "Service charges",
    "Fees for city administrative services, mainly the 2% fee the city charges other districts for collecting sales tax and the tax collection fees, plus zoning, planning and utility regulatory fees.")
src("harrahs-city-support", "Harrah's city support payment", "Service charges",
    "A separate annual payment from Harrah's casino for city services; printed separately only from 2025 (earlier books hold it inside 'general-government-charges').")
src("indirect-cost-recoveries", "Indirect cost recoveries", "Service charges",
    "Money the General Fund recovers from grants and other funds for the administrative overhead it provides to them.")
src("service-charges-other", "Other service charges", "Service charges",
    "Small charges: streets and towing, parks and parkways, state highway maintenance, and a few lines printed under this heading that are state aid.")

# ---- Fines and forfeits ---------------------------------------------------------
src("traffic-camera-fines", "Traffic camera and red-light fines", "Fines and forfeits",
    "Fines from automated red-light and speed camera tickets.")
src("parking-and-traffic-fines", "Parking tickets and traffic/municipal court fines", "Fines and forfeits",
    "Fines paid on parking tickets, traffic violations and municipal court cases; books split these differently (2005-06 print them as one line, and the 2025+ 'Traffic Fines' line has the dollar amounts of the earlier 'Parking Ticket Collections' line), so they are one rev_id.")
src("fines-other", "Other fines and forfeits", "Fines and forfeits",
    "Smaller fines: health-code violations, abandoned vehicle impound fees, administrative adjudication fees and demolition lien penalties.")

# ---- Miscellaneous --------------------------------------------------------------
src("interest-earnings", "Interest earnings", "Miscellaneous",
    "Interest the city earns on cash balances and bond funds.")
src("rents-royalties", "Rents and royalties", "Miscellaneous",
    "Rent on city-owned buildings and land, and landfill and parking royalties.")
src("settlement-judgment-fund", "Settlement and judgment fund", "Miscellaneous",
    "A transfer to cover legal settlements and judgments, printed only in the 2008-2014 books (about $20 million in 2004).")
src("reimbursements-and-other-misc", "Reimbursements and other miscellaneous", "Miscellaneous",
    "Catch-all of reimbursements from other agencies, insurance proceeds, asset sales, contributions and one-time receipts (2015 includes a $36 million BP oil-spill settlement).")

# ---- Other financing sources ----------------------------------------------------
src("fund-balance-use", "Use of fund balance", "Other financing sources",
    "Savings from earlier years used to balance the budget; the 2012 actual of $195.9 million is not savings but pension-bond refinancing proceeds, and the 2023 figure appears to include one-time American Rescue Plan (ARPA) money.")
src("transfers-in", "Transfers from other funds", "Other financing sources",
    "Money moved into the General Fund from other city funds.")
src("fema-disaster-loan", "FEMA community disaster loan", "Other financing sources",
    "Federal disaster loan proceeds after Hurricane Katrina (2005-2009).")
src("go-zone-payment", "Gulf Opportunity Zone payment", "Other financing sources",
    "Payments tied to the Gulf Opportunity Zone program after Katrina (2006-2009).")
src("other-financing-other", "Other financing and adjustments", "Other financing sources",
    "Other adjustments, HUD Section 108 loan draws and small bond-related transfers.")

# ---- Non-General-Fund -----------------------------------------------------------
OF = "Other funds"
src("federal-grants", "Federal grants", OF,
    "Money from federal agencies for specific programs (health, public safety, transportation, disaster recovery), restricted to those uses.")
src("state-local-grants", "State and local grants", OF,
    "State and foundation grants for specific programs; the books print these as State Grants, then State and Local Foundation Grants.")
src("hud-grants", "Housing and Urban Development (HUD) grants", OF,
    "Federal housing and community development money such as CDBG and HOME block grants.")
src("local-law-enforcement-grants", "Local law enforcement grants", OF,
    "Small state grants for local police programs from the Governor's office.")
src("self-generated", "Self-generated funds", OF,
    "Small funds that departments raise themselves and may spend on their own programs.")
src("special-revenue-trust-funds", "Special revenue and trust funds", OF,
    "Dedicated-purpose and trust funds not listed separately; the 2025+ books print this as 'Self Generated, Spc Rev, Trust Funds'.")
src("library-funds", "Library funds", OF,
    "The library's dedicated property tax millage plus library fines and fees.")
src("downtown-development-district", "Downtown Development District", OF,
    "A special property tax paid by downtown property owners for district services; dropped out of the budget as 'Exclude from Budget' in 2026.")
src("economic-development-fund", "Economic development fund", OF,
    "Property tax set aside for economic development projects.")
src("housing-improvement-fund", "Housing improvement (housing trust) fund", OF,
    "Property tax set aside to fund neighbourhood housing improvement.")
src("regional-business-park", "New Orleans Regional Business Park", OF,
    "Property tax from the Regional Business Park district (2008-2017).")
src("wisner-land-trust", "Wisner land trust", OF,
    "Proceeds of the Wisner land trust, printed under Mayoral Fellows Program until 2024 and as Wisner Fund from 2025.")
src("grants-and-transfers", "Grants, contributions and fund transfers", OF,
    "A budgeted placeholder for grants and contributions the city expects to receive during the year but cannot yet attribute to a program.")


# =================================================================================
# General Fund rules.  First matching rule wins; (rev_id, regex on normalised line).
# =================================================================================
def norm(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


GF_RULES = {
    "property": [
        ("property-tax-police-fire-dedicated", r"police fire|dedicated fire|dedicated police|police 5 millage|fire 2 5 millage|rfire|fire 5 millage|special real property"),
        ("property-tax-prior-year", r"^(nord|parkway|streets).*prior"),
        ("property-tax-nord-dedicated", r"\bnord\b|nordc|rec devel"),
        ("property-tax-parkway-dedicated", r"parkway|parks and parkways"),
        ("property-tax-streets-dedicated", r"^streets"),
        ("property-tax-interest-penalties", r"interest"),
        ("property-tax-prior-year", r"prior"),
        ("property-tax-general", r"."),
    ],
    "other taxes": [
        ("sales-tax-motor-vehicle", r"^motor vehicle tax"),
        ("sales-tax-general", r"^sales tax|general sales and use|food and prescribed|direct marketer|airport tax|sewerage and water board tax|tax sales cost"),
        ("hotel-motel-tax", r"hotel"),
        ("utility-tax", r"^utility tax"),
        ("parking-tax", r"^parking tax"),
        ("document-transaction-tax", r"^documents? transaction"),
        ("other-taxes-misc", r"."),
    ],
    "licenses": [
        ("franchise-fees-utilities", r"franchise"),
        ("building-permits", r"^building permits?$|demolition permit|use and occupancy"),
        ("occupational-licenses", r"^occupational licenses|insurance premium tax"),
        ("alcoholic-beverage-permits", r"^alcohol"),
        ("short-term-rental-permits", r"short term rental|nstr"),
        ("taxi-tour-rideshare-licenses", r"taxi|tour guide|tourguide|tour planner|transportation network|cpnc|driver s delinquent|duplicate tag|inspection delinquency"),
        ("motor-vehicle-permits", r"^motor vehicle|brake tag"),
        ("safety-electrical-mechanical-permits",
         r"safety electric|electrical|mechanical|air conditioning|gas fitters|stationary engineer|hoisting|plumbing|annual insp|^sign "),
        ("licenses-permits-other", r"."),
    ],
    "service": [
        ("sanitation-service-charge", r"^sanitation"),
        ("parking-meters", r"parking meters"),
        ("emergency-medical-fees", r"^ems |ambulance"),
        ("harrahs-city-support", r"harrah"),
        ("public-safety-charges", r"^public safety$|airport fire|police record|fire prevention|hazardous material|false alarm|coroner|department of public safety|id charges"),
        ("indirect-cost-recoveries", r"^indirect costs"),
        ("general-government-charges", r"^general government|tax collection|avt collection|plan processing|zoning|utility regulatory|map change|maps|sales maps|appeals bd|non conforming|moratorium|administrative fee|credit card|bad check|redemption cost|shelter rentals"),
        ("service-charges-other", r"."),
    ],
    "fines": [
        ("traffic-camera-fines", r"red signal|red light|camera"),
        ("parking-and-traffic-fines", r"parking ticket|^traffic fines|traffic and municipal|municipal court|admin adjud|demolition lien"),
        ("fines-other", r"."),
    ],
    "interg": [
        ("harrahs-casino-rent", r"harrah"),
        ("state-revenue-sharing", r"state revenue sharing|revenue sharing dedicated"),
        ("state-aid-video-poker", r"video poker"),
        ("state-aid-medicaid", r"medicaid"),
        ("parish-transportation-fund", r"parish transp"),
        ("state-supplemental-pay", r"supplemental pay"),
        ("intergovernmental-other", r"."),
    ],
    "misc": [
        ("settlement-judgment-fund", r"settlement and judgment"),
        ("interest-earnings", r"^interest|lamp interest|^2013 interest"),
        ("rents-royalties", r"rents and royalties|building and grounds rents|landfill royalties|parking royalties"),
        ("reimbursements-and-other-misc", r"."),
    ],
    "other fin": [
        ("fund-balance-use", r"fund balance"),
        ("transfers-in", r"transfer from other funds|tax improv bds"),
        ("fema-disaster-loan", r"fema loan"),
        ("go-zone-payment", r"gulf outlet"),
        ("other-financing-other", r"."),
    ],
}

# category heading (normalised) -> rule group
def cat_group(c):
    n = norm(c)
    if n.startswith("property"):
        return "property"
    if n.startswith("other taxes") or n in ("taxes", "other tax"):
        return "other taxes"
    if n.startswith("licenses"):
        return "licenses"
    if n.startswith("service"):
        return "service"
    if n.startswith("fines"):
        return "fines"
    if n.startswith("intergov"):
        return "interg"
    if n.startswith("misc"):
        return "misc"
    if n.startswith("other financing"):
        return "other fin"
    return None


# Lines printed with no category heading (2022-2024 books, continuation page): all L&P.
BLANK_CAT_GROUP = "licenses"

# lines whose printed heading is not where the money belongs
LINE_OVERRIDES = [
    # (group, regex on norm line, rev_id, note)
    ("service", r"^sales tax collection", "general-government-charges", ""),
    ("service", r"^state aid other", "service-charges-other",
     "2021+ books: small state aid line printed under Service Charges; kept inside the printed Service Charges subtotal."),
    ("service", r"^orleans parish comm", "service-charges-other", ""),
    ("service", r"^streets other", "service-charges-other", ""),
    ("service", r"^cultural and recreation|^parks and parkways?$", "service-charges-other", ""),
    ("misc", r"federal stimulus", "intergovernmental-other", "Federal stimulus payment printed under Miscellaneous."),
    ("misc", r"^transfer from rta", "reimbursements-and-other-misc", "Transfer from the Regional Transit Authority, printed under Miscellaneous."),
    ("misc", r"riverboat gaming", "reimbursements-and-other-misc", "Casino riverboat gaming fee (2004-2008 era); under 1% of General Fund so lumped."),
    ("misc", r"^fema admin", "reimbursements-and-other-misc", ""),
]

NOTES = {
    ("fines", r"^traffic fines$"):
        "UNCERTAIN name: 2025+ 'Traffic Fines' has the same dollars as the older 'Parking Ticket Collections' (FY2024 4,660,000); parking and traffic fines are one rev_id because books split them inconsistently.",
    ("interg", r"^state aid other$"):
        "2021+ books: other state aid; 2025 book FY2024 'Severance Tax Equalization' 150K matches this line's FY2024 150K.",
    ("service", r"^public safety$"):
        "Pre-2025 lump: includes EMS ambulance fees and airport fire suppression (matched on FY2024 amounts).",
    ("service", r"^general government"):
        "Pre-2025 lump: includes Harrah's city support and tax-collection fees (matched on FY2024 amounts).",
    ("licenses", r"^other licenses and permits$"):
        "2021-2024 lump: includes short-term rental permits, printed separately only from the 2025 books.",
    ("licenses", r"^occupational licenses"): "",
    ("other taxes", r"airport tax|sewerage and water board tax|tax sales cost"):
        "Matched by amount: 2025 book FY2024 sales-tax lines + these three = 2024 book 'Sales Tax' FY2024 (258,201,214).",
    ("licenses", r"insurance premium tax"):
        "Matched by amount: 2024 book Occupational Licenses FY2024 12,089,133 = 2025 book occupational 8.92M + this line 3.17M.",
    ("licenses", r"^sign "):
        "Matched by amount to the older Safety/Electrical/Mechanical line (FY2024).",
    ("licenses", r"driver s delinquent|duplicate tag|inspection delinquency"):
        "Matched by amount to the older Taxi Tourguide Licenses line (FY2024 5,831,768).",
    ("licenses", r"demolition permit|use and occupancy"):
        "Matched by amount to the older Building Permits line (FY2024 5,670,000).",
    ("property", r"special real property"):
        "Matched by amount to the older Police/Fire Dedicated Mills line (FY2024 difference 179,275 = 179,158 + 117).",
    ("property", r"^(nord|parkway|streets).*prior"):
        "Matched by amount to the older Prior Year Property line (FY2024 2,493,769 = sum of these prior-year lines).",
    ("fines", r"admin adjud|demolition lien"):
        "Grouped with court fines: 2021-24 books print them inside 'Traffic and Municipal Court' (FY2024 8,085 difference).",
    ("other fin", r"tax improv bds"):
        "Matched by amount to the older Transfer From Other Funds line (FY2024 60,000).",
    ("other taxes", r"^sales tax$"): "Pre-2025 single line; 2025+ prints three lines (general, food and drug, direct marketer) which are lumped here. FY2004-2007 and, in books through 2013, FY2008-2011 include hotel/motel and vehicle tax (2014 book FY2008: sales 119.8M + hotel 9.3M + vehicle 8.5M = 137.6M = 2013 book Sales Tax).",
}

# ---- Non-General-Fund rules on fund_name ---------------------------------------
OF_RULES = [
    ("wisner-land-trust", r"mayoral fellows|wisner"),
    ("federal-grants", r"federal grants"),
    ("hud-grants", r"housing and urban|housing urban|housing & urban"),
    ("local-law-enforcement-grants", r"lle grants|local law enf"),
    ("state-local-grants", r"state grants|state and local foundation|other state grants"),
    ("housing-improvement-fund", r"housing trust|housing improvement"),
    ("library-funds", r"library"),
    ("downtown-development-district", r"downtown development|exclude from budget"),
    ("economic-development-fund", r"economic development"),
    ("regional-business-park", r"regional business park"),
    ("grants-and-transfers", r"grants contr"),
    ("special-revenue-trust-funds", r"additional special revenues|self generated spc rev|special revenue funds"),
    ("self-generated", r"self generated"),
]
OF_NOTES = {
    "special-revenue-trust-funds": "",
}


def map_gf(book, cat, line, ctx):
    """ctx['hotel_books']: books that print a separate hotel/motel tax line."""
    g = cat_group(cat)
    n = norm(line)
    year = int(book[:4])
    # 'State Aid Other' 2008-2021 books: state supplemental pay for police and fire
    if re.search(r"^state aid other$", n) and g in ("interg", "service") and year <= 2021:
        return ("state-supplemental-pay",
                "Footnote in 2016/2017/2018 books: 'State Aid Other' is supplemental pay for Police and Fire ($10-13M 2009-2013); "
                "line moved to Service Charges in 2015. 2018-2021 books assumed to be the same line (2021 book FY2020 4,300,000 = 2020 book's).")
    for og, rx, rid, nt in LINE_OVERRIDES:
        if re.search(rx, n) and (g == og):
            return rid, nt
    if g is None:
        g = BLANK_CAT_GROUP if cat.strip() == "" else None
    if g is None:
        raise ValueError("unmapped category %r" % cat)
    for rid, rx in GF_RULES[g]:
        if re.search(rx, n):
            note = ""
            for (ng, nrx), nt in NOTES.items():
                if ng == g and re.search(nrx, n) and nt:
                    note = nt
            return rid, note
    raise ValueError("no rule for %r %r" % (cat, line))


def map_of(fund, line):
    n = norm(fund)
    for rid, rx in OF_RULES:
        if re.search(rx, n):
            note = ""
            if "exclude from budget" in n:
                note = "2026+ books move the Downtown Development District out of the budget; amount equals the DDD figure."
            if "self generated spc rev" in n:
                note = "2025+ name for what 2012-2024 books print as 'Additional Special Revenues & Trust Funds' (same dollars in FY2024)."
            return rid, note
    raise ValueError("unmapped other fund %r" % fund)


# Groups of rev_ids that some books print as one line and others as several. A series that is
# comparable across all years is the sum of the members. (rollup_id, [rev_ids], reason)
ROLLUPS = [
    ("sales-and-related-taxes", ["sales-tax-general", "sales-tax-motor-vehicle", "hotel-motel-tax"],
     "FY2004-2007 in every book, and FY2008-2011 in the 2011-2013 books, print hotel/motel and motor vehicle tax inside Sales Tax; the 2014+ books split them for FY2008 on (FY2008: 119.8+9.3+8.5 = 137.6M)."),
    ("state-aid-lines-2011-book", ["intergovernmental-other", "state-aid-video-poker", "state-supplemental-pay"],
     "The 2011 book's FY2008 column puts video poker inside State Aid Other; later books show it separately."),
    ("transfers-and-fund-balance-2009-book", ["fund-balance-use", "transfers-in"],
     "The 2009 book's FY2005 column reclassifies $21.2M from Transfers to Fund Balance; all other books keep it in Transfers."),
    ("public-safety-incl-ems", ["public-safety-charges", "emergency-medical-fees"],
     "Books before 2025 print ambulance fees inside Public Safety; the 2025+ books split them (FY2024: 18.74M = 6.59M + 12.14M)."),
    ("general-government-incl-harrahs-support", ["general-government-charges", "harrahs-city-support"],
     "Books before 2025 print Harrah's city support inside General Government; the 2025+ books split it (FY2024: 23.61M = 17.61M + 6.0M)."),
    ("licenses-other-incl-short-term-rentals", ["licenses-permits-other", "short-term-rental-permits"],
     "Books before 2025 print short-term rental permits inside Other Licenses and Permits (FY2024: 8.35M = 1.54M + 6.81M)."),
]


def main():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "extracted", "*", "revenues.csv"))):
        rows += list(csv.DictReader(open(f, encoding="utf-8")))
    keys = collections.OrderedDict()
    for r in rows:
        keys[(r["book_id"], r["fund_group"], r["fund_name"], r["category"], r["line_name"])] = 1
    hotel_books = {r["book_id"] for r in rows
                   if r["fund_group"] == "general_fund" and "hotel" in r["line_name"].lower()}
    ctx = {"hotel_books": hotel_books}
    out = []
    for (b, fg, fn, cat, ln) in keys:
        if fg == "general_fund":
            rid, note = map_gf(b, cat, ln, ctx)
        else:
            rid, note = map_of(fn, ln)
        out.append([b, fg, fn, cat, ln, rid, note])
    out.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "revenue_aliases.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["book_id", "fund_group", "fund_name", "category_printed", "line_name", "rev_id", "note"])
        w.writerows(out)
    used = {r[5] for r in out}
    with open(os.path.join(OUT, "revenue_sources.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rev_id", "name", "category", "plain_description"])
        for rid, (nm, ct, ds) in SOURCES.items():
            if rid in used:
                w.writerow([rid, nm, ct, ds])
    with open(os.path.join(OUT, "revenue_rollups.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rollup_id", "rev_id", "reason"])
        for rid_, members, why in ROLLUPS:
            for m in members:
                assert m in used, m
                w.writerow([rid_, m, why])
    unused = [r for r in SOURCES if r not in used]
    print("alias rows", len(out), "rev_ids used", len(used), "defined-but-unused", unused)


if __name__ == "__main__":
    main()
