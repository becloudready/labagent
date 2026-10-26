"""Invented names and the pools the builtin generator draws from. No real institution appears here: DENYLIST is checked by tests
against every file the pack ships and every table it generates."""
from __future__ import annotations

import re

FIRST = ["Aiden", "Amara", "Anika", "Arjun", "Beatrice", "Bram", "Camila", "Chloe", "Dara", "Declan", "Elena", "Emeka", "Farah", "Felix",
         "Gwen", "Hamid", "Hana", "Hugo", "Imani", "Isaac", "Jonah", "Julia", "Kai", "Keira", "Lars", "Leila", "Liam", "Lucia", "Mateo",
         "Maya", "Nadia", "Nolan", "Olga", "Omar", "Priya", "Quinn", "Rafael", "Rhea", "Sam", "Selma", "Tariq", "Thea", "Uma", "Viktor",
         "Wren", "Xavier", "Yara", "Zane", "Ines", "Joaquin", "Katya", "Malik", "Noor", "Owen", "Petra", "Ravi", "Sofia", "Tomas",
         "Una", "Vera", "Wes", "Yusuf", "Zoe", "Abel", "Bianca", "Cyrus", "Dalia", "Eli", "Freya", "Gideon", "Hallie", "Idris", "Jade"]
LAST = ["Abara", "Alder", "Banerjee", "Brightwater", "Caldwell", "Chen", "Dalgleish", "Delacroix", "Eriksen", "Fairbairn", "Garnier",
        "Haddad", "Ishikawa", "Jovanovic", "Kowalczyk", "Lindqvist", "Moreau", "Nakamura", "Okafor", "Petrov", "Quill", "Rosario",
        "Sandhu", "Thorne", "Ueda", "Valdez", "Whitlock", "Yilmaz", "Zielinski", "Ashworth", "Boyle", "Cordero", "Dube", "Everhart",
        "Fontaine", "Grewal", "Holloway", "Ibarra", "Jansen", "Kapoor", "Larkin", "Marchetti", "Novak", "Ocampo", "Pereira", "Raines",
        "Sorensen", "Tremblay", "Underhill", "Vasquez", "Wexler", "Yamada", "Zhou", "Armitage", "Bastien", "Costa", "Doherty", "Elwood"]
BUSINESS_STEMS = ["Birchwood", "Cedar Hollow", "Driftline", "Eastgate", "Fernbrook", "Granite Peak", "Harbourview", "Ironbridge", "Juniper",
                  "Kestrel", "Lakeshore", "Millstone", "Northfield", "Oakridge", "Pinecrest", "Quarry Lane", "Redfern", "Stonehaven",
                  "Timberline", "Upland", "Valleyfield", "Westmere", "Yarrow", "Alder Creek", "Brightside", "Copperfield", "Dunmore"]
BUSINESS_KINDS = [
    # (industry, name suffix, mean monthly revenue, cash intensive)
    ("restaurant", "Kitchen Ltd.", 42000, True), ("landscaping", "Landscaping Inc.", 26000, True), ("auto repair", "Auto Service Ltd.", 38000, True),
    ("retail clothing", "Apparel Inc.", 34000, False), ("construction", "Builders Ltd.", 90000, False), ("dental clinic", "Dental Group Inc.", 70000, False),
    ("convenience store", "Market Ltd.", 52000, True), ("trucking", "Freight Ltd.", 80000, False), ("consulting", "Advisory Inc.", 45000, False),
    ("bakery", "Bakery Ltd.", 21000, True), ("software services", "Digital Inc.", 60000, False), ("import and export", "Trading Ltd.", 75000, False),
]
OCCUPATIONS = [
    # (occupation, mean monthly income)
    ("teacher", 5200), ("nurse", 6100), ("software developer", 8200), ("electrician", 6400), ("student", 1400), ("retired", 3100),
    ("truck driver", 5600), ("accountant", 7000), ("server", 2600), ("warehouse worker", 3600), ("civil engineer", 8800),
    ("pharmacist", 9200), ("graphic designer", 5000), ("paramedic", 5900), ("sales associate", 3200), ("plumber", 6000),
]
EMPLOYERS = ["Northbridge School Board", "Lakeview Health Region", "Crestline Logistics", "Ardent Software", "Brightpath Staffing",
             "Summit Retail Group", "Harlow Construction", "Clearwater Pharmacy", "Ostrander Engineering", "Pembroke Foods",
             "Tidewater Transit", "Greenleaf Grocers", "Ridgeway Manufacturing", "Silverline Dental", "Fairhaven Care Homes"]
CITIES = {"CA": ["Maple Ridge", "Kelowna", "Red Deer", "Guelph", "Moncton", "Brandon", "Kamloops", "Sudbury"],
          "US": ["Spokane", "Boise", "Duluth", "Dayton", "Asheville", "Fresno", "Tulsa", "Madison"]}
FOREIGN = ["GB", "IN", "PH", "MX", "AE", "TR", "NG", "CN", "PA", "CY"]
COUNTRY_NAMES = {"CA": "Canada", "US": "the United States", "GB": "the United Kingdom", "IN": "India", "PH": "the Philippines", "MX": "Mexico",
                 "AE": "the United Arab Emirates", "TR": "Turkey", "NG": "Nigeria", "CN": "China", "PA": "Panama", "CY": "Cyprus"}
HIGH_RISK_COUNTRIES = {"AE", "TR", "NG", "PA", "CY"}
BRANCHES = ["B01", "B02", "B03", "B04", "B05"]

# Real institutions and payment brands that must never appear in generated data or shipped documents.
DENYLIST = [
    r"royal bank", r"\brbc\b", r"\btd bank\b", r"toronto[- ]dominion", r"scotiabank", r"bank of nova scotia", r"\bbmo\b", r"bank of montreal",
    r"\bcibc\b", r"national bank", r"desjardins", r"tangerine", r"\bhsbc\b", r"jp ?morgan", r"\bchase\b", r"wells fargo", r"bank of america",
    r"citibank", r"citigroup", r"goldman", r"morgan stanley", r"u\.?s\.? bancorp", r"u\.?s\.? bank", r"\bpnc\b", r"truist", r"capital one",
    r"barclays", r"santander", r"deutsche bank", r"\bubs\b", r"credit suisse", r"western union", r"paypal", r"\bvisa\b", r"mastercard",
    r"\binterac\b", r"\bzelle\b", r"coast capital", r"vancity", r"servus", r"meridian credit",
]
DENY_RE = re.compile("|".join(DENYLIST), re.I)


def real_bank_hits(text: str):
    return sorted({m.group(0).lower() for m in DENY_RE.finditer(text)})
