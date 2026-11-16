#!/usr/bin/env python3
"""
Nelson Court meal-plan bot (Ohio University).

Pulls the day's Nelson Court menu + per-item nutrition labels from
ohio.edu/food/menus and builds a breakfast/lunch/dinner plan aimed at a
"maingain" (slow lean gain / recomposition) with an overall-health bias:
high protein, a small calorie surplus, plenty of fiber and veg, and limits
on sodium, saturated fat, added sugar, fried food and processed meat.

Standard library only - no pip installs needed.

Usage:
    python nelson_bot.py                 # plan for today
    python nelson_bot.py --date 2026-11-10
    python nelson_bot.py --setup         # enter your stats (weight, height, ...)
    python nelson_bot.py --meals lunch,dinner
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from fractions import Fraction
from pathlib import Path

BASE = "https://www.ohio.edu"
MENU_URL = BASE + "/food/menus?display=getmenu&location={loc}&dtdate={date}&meal={meal}"
DEFAULT_LOCATION = "NELSON COURT"
# OU's location names (as used on ohio.edu/food/menus) -> short names for the plan.
PLACE_NAMES = {
    "NELSON COURT": "Nelson Court",
    "THE DISTRICT ON WEST GREEN": "The District (Boyd)",
    "SHIVELY COMMONS AND EARL'S COOP": "Shively",
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def location_for(date, profile, override=None, meal=None):
    """Which dining hall to plan this meal for. In the profile, a weekday can name one hall for
    the whole day ("tuesday": "HALL") or one per meal ("tuesday": {"lunch": "HALL"})."""
    if override:
        return override.upper()
    by_day = {k.lower(): v for k, v in profile.get("locations_by_weekday", {}).items()}
    choice = by_day.get(WEEKDAYS[date.weekday()])
    if isinstance(choice, dict):
        choice = {k.lower(): v for k, v in choice.items()}.get(meal)
    return (choice or profile.get("location") or DEFAULT_LOCATION).upper()


def place_name(location):
    return PLACE_NAMES.get(location, location.title())
MEAL_IDS = {"breakfast": 0, "lunch": 1, "dinner": 2}
HERE = Path(__file__).resolve().parent
PROFILE_PATH = HERE / "profile.json"
WEIGHTS_PATH = HERE / "weights.json"
CACHE_PATH = HERE / ".cache" / "nutrition.json"
PLANS_DIR = HERE / "plans"
UA = "Mozilla/5.0 (compatible; NelsonMealPlanner/1.0; personal use)"

ACTIVITY = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "very": 1.725, "athlete": 1.9}

DEFAULT_PROFILE = {
    "weight_lb": 160,
    "height_in": 70,
    "age": 19,
    "sex": "male",              # used only for the BMR formula: "male" or "female"
    "activity": "moderate",     # sedentary | light | moderate | very | athlete
    "surplus_kcal": 250,        # maingain: +200-300 kcal (Helms 2023; Iraki 2019)
    "gain_pct_low": 0.25,       # target weekly gain, % of body weight
    "gain_pct_high": 0.35,
    "produce_target_g": 600,    # fruit + veg grams per day
    "dessert_max_kcal": 300,
    "protein_g_per_lb": 0.9,
    "fat_pct": 0.27,            # share of calories from fat
    "sodium_limit_mg": 2300,
    "meals": ["breakfast", "lunch", "dinner"],
    "meal_split": {"breakfast": 0.25, "lunch": 0.35, "dinner": 0.40},
    "avoid_allergens": [],      # any of: P TN SH F M E S SS
    "avoid_keywords": [],       # e.g. ["pork", "mushroom"]
    "vegetarian": False,
    "vegan": False,
    "favorites": [],            # words in item names you like, e.g. ["chicken", "penne", "pizza"]
    "location": "NELSON COURT",   # default dining hall (name as on ohio.edu/food/menus)
    "locations_by_weekday": {},   # e.g. {"tuesday": {"lunch": "THE DISTRICT ON WEST GREEN"}}
    "allow_dessert": False,
    "desserts_per_day": 1,      # used when allow_dessert is true (planned at the last meal)
}

# --------------------------------------------------------------------------- fetching

START = time.monotonic()
TIME_LIMIT = 110.0       # seconds; overridden by --time-limit
REQUEST_TIMEOUT = 45.0   # per request; --prefetch raises it (ohio.edu can take minutes per page)


def time_left():
    return TIME_LIMIT - (time.monotonic() - START)


def fetch(url):
    """One GET whose timeout never runs past the overall time limit."""
    timeout = min(REQUEST_TIMEOUT, time_left() - 3)
    if timeout < 3:
        raise TimeoutError("out of time")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_menu(page):
    table = re.search(r'id="edit-menu-table".*?</table>', page, re.S)
    if not table:
        return []
    items, station = [], "?"
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table.group(0), re.S):
        cat = re.search(r'colspan="2"[^>]*>(.*?)</td>', row, re.S)
        if cat:
            station = " ".join(w.capitalize() for w in strip_tags(cat.group(1)).strip("- ").split())
            continue
        a = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', row, re.S)
        if not a:
            continue
        href = html.unescape(a.group(1))
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        name = strip_tags(a.group(2))
        items.append({
            "name": name,
            "station": station,
            "tags": re.findall(r"legend-icon-([A-Z!]+)", row),
            "url": BASE + href if href.startswith("/") else href,
            "key": qs.get("RecNumAndPort", [name])[0],
        })
    return items


def parse_label(page):
    serving = re.search(r'class="serving-size">\s*Serving Size:\s*(.*?)\s*<', page, re.S)
    cal = re.search(r'class="calories">\s*Calories\s*([\d.]+)', page)
    if not cal:
        return None
    vals = {}
    for txt in re.findall(r'percentvalue-(?:main|sub)-item">([^<]+)<', page):
        m = re.match(r"\s*(.+?)\s+([\d.]+)\s*(m?g)\s*$", html.unescape(txt))
        if m:
            vals[m.group(1).lower()] = float(m.group(2))
    return {
        "serving": serving.group(1).strip() if serving else "1 serving",
        "kcal": float(cal.group(1)),
        "fat": vals.get("fat", 0.0),
        "sat_fat": vals.get("saturated fat", 0.0),
        "sodium": vals.get("sodium", 0.0),
        "carbs": vals.get("carbohydrates", 0.0),
        "fiber": vals.get("dietary fiber", 0.0),
        "sugar": vals.get("total sugars", 0.0),
        "added_sugar": vals.get("added sugar", 0.0),
        "protein": vals.get("protein", 0.0),
    }


MENU_CACHE = HERE / ".cache" / "menus"


def fetch_menu(date, meal, use_cache=True, location=DEFAULT_LOCATION):
    """Menus are cached per day, so re-running for the same date is instant.
    The OU site sometimes serves the page without the menu table; retry until
    there is time left for labels + planning."""
    slug = "" if location == DEFAULT_LOCATION else re.sub(r"[^a-z0-9]+", "-", location.lower()).strip("-") + "-"
    path = MENU_CACHE / f"{date.isoformat()}-{slug}{meal}.json"
    if use_cache and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    url = MENU_URL.format(loc=urllib.parse.quote(location), date=date.strftime("%m/%d/%Y"), meal=MEAL_IDS[meal])
    attempt = 0
    while time_left() > 25:
        attempt += 1
        try:
            items = parse_menu(fetch(url))
            if items:
                MENU_CACHE.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(items), encoding="utf-8")
                return items
        except Exception:
            pass
        time.sleep(min(2 * attempt, 6))
    print(f"  (no {meal} menu came back for {date} - not served, or ohio.edu is having trouble)", file=sys.stderr)
    return []


def fetch_label(url):
    attempt = 0
    while time_left() > 8:
        attempt += 1
        try:
            lab = parse_label(fetch(url))
            if lab:
                return lab
        except Exception:
            pass
        if attempt >= 3:
            break
        time.sleep(2 * attempt)
    return None


PRIORITY = [
    re.compile(r"chicken|beef|turkey|pork|salmon|fish|tuna|cod|pollock|tilapia|shrimp|egg|tofu|steak|"
               r"burger|meatball|chili|bean|lentil|yogurt|cottage|ham\b|sausage|brisket|carnitas", re.I),
    re.compile(r"rice|pasta|penne|noodle|potato|quinoa|oat|bread|bagel|tortilla|wrap|toast|bun|pancake|waffle|grits|biscuit", re.I),
]


def fetch_priority(it):
    for i, rx in enumerate(PRIORITY):
        if rx.search(it["name"]):
            return i
    return len(PRIORITY)


def load_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=0), encoding="utf-8")


def attach_nutrition(all_items, profile, use_cache=True):
    cache = load_cache() if use_cache else {}
    todo = {}
    # Most important labels first (proteins, then carbs, then the rest), and never
    # spend time on items the planner would reject by name anyway.
    for it in sorted(all_items, key=fetch_priority):
        if it["key"] not in cache and name_allowed(it, profile):
            todo.setdefault(it["key"], it["url"])
    if todo:
        print(f"Fetching {len(todo)} new nutrition labels (cached after this, "
              f"{max(0, time_left()):.0f}s left in the time limit) ...", file=sys.stderr)
        # The OU server returns blank labels under heavy parallel load, so keep
        # concurrency modest; fetch_label retries blanks while time remains.
        ex = cf.ThreadPoolExecutor(max_workers=3)
        futs = {ex.submit(fetch_label, url): key for key, url in todo.items()}
        done = 0
        try:
            for f in cf.as_completed(futs, timeout=max(1.0, time_left() - 4)):
                done += 1
                lab = f.result()
                if lab:
                    cache[futs[f]] = lab
                if done % 15 == 0:
                    print(f"  {done}/{len(futs)}", file=sys.stderr)
        except cf.TimeoutError:
            print(f"  time limit reached: planning with {done}/{len(futs)} new labels "
                  f"(run again later to fill in the rest)", file=sys.stderr)
        ex.shutdown(wait=False, cancel_futures=True)
        save_cache(cache)
    for it in all_items:
        it["n"] = cache.get(it["key"])

# --------------------------------------------------------------------------- targets

def compute_targets(p, kcal_adjust=0):
    """Day-one estimate (Mifflin-St Jeor x activity) + surplus, corrected over time by the
    weight-trend adjustment. Research summary: reports/Diet for advanced male lifters.md."""
    kg = p["weight_lb"] * 0.4536
    cm = p["height_in"] * 2.54
    bmr = 10 * kg + 6.25 * cm - 5 * p["age"] + (5 if str(p["sex"]).lower().startswith("m") else -161)
    tdee = bmr * ACTIVITY.get(p["activity"], 1.55)
    kcal = tdee + p["surplus_kcal"] + kcal_adjust
    protein = p["weight_lb"] * p["protein_g_per_lb"]
    fat = kcal * p["fat_pct"] / 9
    carbs = max(0.0, (kcal - protein * 4 - fat * 9) / 4)
    return {
        "bmr": bmr, "tdee": tdee, "kcal": kcal, "protein": protein, "carbs": carbs, "fat": fat,
        "fiber": 14 * kcal / 1000,                      # no upper cap (Reynolds 2019)
        "fat_floor": max(0.20 * kcal / 9, 0.8 * kg),    # low-fat diets lower testosterone
        "produce_g": p.get("produce_target_g", 600),     # fruit + veg (Aune 2017)
        "kcal_adjust": kcal_adjust,
        "sodium": p["sodium_limit_mg"],
        "sat_fat": kcal * 0.10 / 9,
        "added_sugar": kcal * 0.10 / 4,
    }

# --------------------------------------------------------------------------- classification

RX = lambda s: re.compile(s, re.I)
VEG = RX(r"broccoli|carrot|cauliflower|celery|lettuce|romaine|spinach|kale|greens|green bean|zucchini|squash|"
         r"\bpeppers?\b|mushroom|cucumber|tomato|vegetable|veggie|brussel|cabbage|asparagus|\bpeas?\b|edamame|"
         r"\bbeets?\b|sprouts|eggplant|okra|collard|spring mix")
FRUIT = RX(r"apple|banana|orange|berr|melon|grape|pineapple|\bfruit|peach|\bpears?\b|mango|kiwi|clementine|cantaloupe")
NOT_PRODUCE = RX(r"yogurt|pie\b|cake|crisp|cobbler|muffin|sauce|vinaigrette|dressing|pancake|waffle|juice|jelly|fried|pizza|noodle|rice")
DESSERT = RX(r"cake|cookie|brownie|\bpie\b|cupcake|pudding|donut|doughnut|blondie|frosting|ice cream|cobbler|"
             r"fudge|candy|cinnamon roll|sweet roll|danish|cheesecake|muffin|pastry|pop.?tart|waffle cone|churro")
CONDIMENT = RX(r"dressing|vinaigrette|mayo|\bmix\b|cones?\b|syrup|relish|ketchup|mustard|\bjelly\b|\bjam\b|"
               r"sprinkles|whipped topping|margarine|^butter|cream cheese|sour cream")
TOPPING = RX(r"sauce|gravy|salsa|hummus|guacamole|cheese sauce|queso")
FRIED = RX(r"fried|fries|breaded|crispy|tots\b|tempura|nugget|tender|wings|fritter|onion ring|corn dog|spring roll|egg roll|hash brown")
PROCESSED = RX(r"salami|sausage|pepperoni|hot dog|bacon|\bham\b|cheddarwurst|bratwurst|bologna|kielbasa|chorizo|\bfranks?\b")
FISH = RX(r"salmon|tuna|trout|cod\b|tilapia|pollock|shrimp|mahi|catfish")


def produce_grams(serving):
    """Grams in one posted serving, for counting fruit + veg."""
    m = re.match(r"\s*([\d/ .]+)\s*(\w+)", serving or "")
    if not m:
        return 80.0
    try:
        qty = float(sum(Fraction(x) for x in m.group(1).split()))
    except (ValueError, ZeroDivisionError):
        return 80.0
    per = {"OZW": 28.35, "OZV": 29.6, "CUP": 150.0, "EACH": 100.0}.get(m.group(2).upper(), 80.0)
    return qty * per


def main_name(name):
    """'Seared Salmon w/ Tropical Relish' -> 'Seared Salmon' (so a garnish doesn't make it a condiment)."""
    return re.split(r"\s+(?:w/|with)\s*", name, maxsplit=1, flags=re.I)[0]


def classify(it, profile):
    n, name, st = it["n"], it["name"], it["station"].lower()
    kcal = n["kcal"]
    head = main_name(name)
    it["dessert"] = "dessert" in st or bool(DESSERT.search(head))
    it["condiment"] = bool(CONDIMENT.search(head)) or ("nutritional information" in st and bool(TOPPING.search(head)))
    it["topping"] = bool(TOPPING.search(head)) and n["protein"] < 10
    produce_ok = kcal <= 150 and n["fat"] <= 8 and not NOT_PRODUCE.search(name)
    it["veg"] = produce_ok and bool(VEG.search(name))
    it["fruit"] = produce_ok and bool(FRUIT.search(name)) and not it["veg"]
    it["protein_src"] = n["protein"] >= 12 and n["protein"] * 4 >= 0.25 * max(kcal, 1)
    it["carb_base"] = n["carbs"] >= 20 and not it["dessert"] and not it["topping"]
    it["fried"] = bool(FRIED.search(name))
    it["processed"] = bool(PROCESSED.search(name))
    it["fish"] = bool(FISH.search(name)) and not it["fried"]
    it["n"] = {**n, "produce_g": produce_grams(n["serving"]) if (it["veg"] or it["fruit"]) else 0.0}

    # Per-serving health penalty (roughly 0..3; negative = bonus).
    pen = 0.04 * n["added_sugar"]
    pen += 0.12 * max(0.0, n["sat_fat"] - 2)
    pen += min(2.0, 0.4 * max(0.0, n["sodium"] / max(kcal, 25) - 1.5))
    pen += 1.3 if it["fried"] else 0
    pen += 1.5 if it["processed"] else 0      # IARC Group 1: keep processed meat near zero
    pen += 0.6 if it["dessert"] else 0
    pen -= min(0.8, 0.08 * n["fiber"])
    pen -= 0.5 if it["fish"] else 0
    pen -= 0.3 if (it["veg"] or it["fruit"]) else 0
    it["favorite"] = any(k.lower() in name.lower() for k in profile.get("favorites", []))
    pen -= 0.6 if it["favorite"] else 0
    it["pen"] = pen

    if it["veg"] or it["fruit"]:
        it["max_serv"] = 3.0
    elif it["dessert"] or it["topping"]:
        it["max_serv"] = 1.0
    else:
        it["max_serv"] = 3.0 if (it["favorite"] and it["protein_src"]) else 2.0

    it["allowed"] = name_allowed(it, profile)


def name_allowed(it, profile):
    """Hard filters that need only the item's name/station/tags (no nutrition label)."""
    name, st, tags = it["name"], it["station"].lower(), set(it["tags"])
    head = main_name(name)
    if CONDIMENT.search(head) or ("nutritional information" in st and TOPPING.search(head)):
        return False
    if ("dessert" in st or DESSERT.search(head)) and not profile.get("allow_dessert"):
        return False
    if set(profile.get("avoid_allergens", [])) & tags:
        return False
    if any(k.lower() in name.lower() for k in profile.get("avoid_keywords", [])):
        return False
    if profile.get("vegan"):
        return "VN" in tags
    if profile.get("vegetarian"):
        return "VT" in tags or "VN" in tags
    return True

# --------------------------------------------------------------------------- optimizer

NUTRS = ("kcal", "protein", "carbs", "fat", "fiber", "sodium", "sat_fat", "added_sugar", "produce_g")
SCALED = NUTRS + ("fat_floor",)  # targets that get split across meals


def totals(sel, by_key):
    t = dict.fromkeys(NUTRS, 0.0)
    for k, s in sel.items():
        n = by_key[k]["n"]
        for f in NUTRS:
            t[f] += n[f] * s
    return t


def meal_cost(sel, by_key, T, used_today):
    if not sel:
        return 1e9
    t = totals(sel, by_key)
    sq = lambda x: x * x
    c = 40 * sq((t["kcal"] - T["kcal"]) / T["kcal"])
    dp = (t["protein"] - T["protein"]) / T["protein"]
    c += 40 * sq(dp) if dp < 0 else 3 * sq(dp)
    c += 8 * sq((t["carbs"] - T["carbs"]) / max(T["carbs"], 1))
    c += 8 * sq((t["fat"] - T["fat"]) / T["fat"])
    c += 10 * sq(max(0.0, T["fiber"] - t["fiber"]) / T["fiber"])
    c += 10 * sq(max(0.0, T["fat_floor"] - t["fat"]) / T["fat_floor"])
    c += 6 * sq(max(0.0, T["produce_g"] - t["produce_g"]) / T["produce_g"])
    for f in ("sodium", "sat_fat", "added_sugar"):
        c += 6 * sq(max(0.0, t[f] - T[f]) / T[f])
    items = [by_key[k] for k in sel]
    c += 0.5 * sum(it["pen"] * sel[it["key"]] for it in items)
    if not any(it["veg"] or it["fruit"] for it in items):
        c += 1.5
    if any(it["topping"] for it in items) and not any(it["carb_base"] or it["protein_src"] for it in items
                                                      if not it["topping"]):
        c += 3.0  # a sauce/gravy needs something to go on
    n_dessert = sum(1 for it in items if it["dessert"])
    if T.get("_dessert") and n_dessert == 0:
        c += 3.0  # you asked for a dessert: treat it as part of the meal
    c += 2.0 * max(0, n_dessert - 1)
    if not any(it["protein_src"] for it in items):
        c += 1.5
    c += 0.05 * len(sel) + 0.25 * max(0, len(sel) - 4)
    c += 0.4 * max(0, len({it["station"] for it in items}) - 2)
    c += 0.6 * sum(1 for k in sel if k in used_today and not by_key[k]["favorite"])
    return c


def local_search(start, cands, by_key, T, used_today):
    sel = dict(start)
    best = meal_cost(sel, by_key, T, used_today)
    for _ in range(80):
        move_best, move_sel = best, None
        # adjust / add / remove by half servings
        for it in cands:
            k = it["key"]
            cur = sel.get(k, 0.0)
            for d in (0.5, -0.5, 1.0):
                ns = cur + d
                if ns < 0 or ns > it["max_serv"] or ns == cur:
                    continue
                if it["dessert"] and ns not in (0, 1):
                    continue  # a dessert is one whole piece/slice, not half
                trial = dict(sel)
                if ns == 0:
                    trial.pop(k)
                else:
                    trial[k] = ns
                c = meal_cost(trial, by_key, T, used_today)
                if c < move_best - 1e-9:
                    move_best, move_sel = c, trial
        # swap one selected item for another
        for k in list(sel):
            for it in cands:
                if it["key"] in sel:
                    continue
                trial = dict(sel)
                s = min(trial.pop(k), it["max_serv"])
                trial[it["key"]] = s
                c = meal_cost(trial, by_key, T, used_today)
                if c < move_best - 1e-9:
                    move_best, move_sel = c, trial
        if move_sel is None:
            break
        best, sel = move_best, move_sel
    return sel, best


def protein_rank(it):
    n = it["n"]
    return n["protein"] * 4 / max(n["kcal"], 1) + 0.004 * n["protein"] - 0.15 * it["pen"]


def plan_meal(items, T, used_today, rng):
    cands = [it for it in items if it.get("n") and it.get("allowed")
             and (T.get("_dessert") or not it["dessert"])
             and not (it["dessert"] and it["n"]["kcal"] > T.get("_dessert_max", 300))]
    if not cands:
        return {}, None
    by_key = {it["key"]: it for it in cands}
    prots = sorted((i for i in cands if i["protein_src"]), key=protein_rank, reverse=True)[:6]
    carbs = sorted((i for i in cands if i["carb_base"]), key=lambda i: i["n"]["fiber"] * 0.2 - i["pen"], reverse=True)[:4]
    produce = sorted((i for i in cands if i["veg"] or i["fruit"]), key=lambda i: i["n"]["fiber"] - i["pen"], reverse=True)[:3]
    starts = []
    for p in prots or [None]:
        for c in carbs or [None]:
            s = {}
            if p: s[p["key"]] = 1.0
            if c: s[c["key"]] = 1.0
            if produce: s[produce[0]["key"]] = 1.0
            if s: starts.append(s)
    for _ in range(8):
        s = {i["key"]: 1.0 for i in rng.sample(cands, min(3, len(cands)))}
        starts.append(s)
    if T.get("_dessert"):
        desserts = sorted((i for i in cands if i["dessert"]), key=lambda i: i["pen"])[:3]
        starts += [{**s, d["key"]: 1.0} for s in list(starts) for d in desserts]
    best_sel, best_cost = {}, float("inf")
    for s in starts:
        sel, c = local_search(s, cands, by_key, T, used_today)
        if c < best_cost:
            best_sel, best_cost = sel, c
    return best_sel, by_key

# --------------------------------------------------------------------------- output

def portion_text(serving, mult):
    m = re.match(r"\s*([\d/ .]+)\s*(.*)", serving)
    unit_names = {"OZW": "oz", "OZV": "fl oz", "EACH": "", "CUP": "cup", "SLICE": "slice", "TBSP": "tbsp"}
    if not m:
        return f"{mult:g} x {serving}"
    try:
        qty = sum(Fraction(x) for x in m.group(1).split())
    except (ValueError, ZeroDivisionError):
        return f"{mult:g} x {serving}"
    unit = m.group(2).strip().upper()
    total = float(qty) * mult
    friendly = unit_names.get(unit, unit.lower())
    amount = f"{total:g} {friendly}".strip() if friendly else f"{total:g}"
    if unit == "EACH":
        amount = f"{total:g} piece" + ("s" if total != 1 else "")
    return amount if mult == 1 else f"{amount} ({mult:g} x {serving})"


def fmt_row(n, mult=1.0):
    return (f"{n['kcal']*mult:5.0f} kcal  P {n['protein']*mult:4.0f}  C {n['carbs']*mult:4.0f}  "
            f"F {n['fat']*mult:4.0f}  fiber {n['fiber']*mult:3.0f}  Na {n['sodium']*mult:5.0f}mg")


def day_notes(T, day):
    notes = []
    gap = T["kcal"] - day["kcal"]
    if gap > 150:
        notes.append(f"You're ~{gap:.0f} kcal short of the maingain target. Close it with a snack: "
                     "Greek yogurt + granola, a PB&J, a glass of milk, trail mix or a banana with peanut butter.")
    pgap = T["protein"] - day["protein"]
    if pgap > 15:
        notes.append(f"Protein is ~{pgap:.0f} g short - add a protein shake, Greek yogurt, milk or extra deli turkey/chicken.")
    if day["sodium"] > T["sodium"]:
        notes.append("Sodium runs high (dining-hall food usually does): drink extra water and skip added sauces/dressings.")
    if day["produce_g"] < 400:
        notes.append(f"Fruit & veg is ~{day['produce_g']:.0f} g - aim for 600 g a day (a big salad-bar plate is "
                     "200-300 g; grab a piece of fruit too).")
    if day["fiber"] < T["fiber"]:
        notes.append("Fiber is under target - add another plate of salad-bar veg or a piece of fruit.")
    notes.append("Portions are by weight (OZW = oz by weight): 4 oz of meat is about the size of your palm; "
                 "1 cup of rice/pasta is about a fist.")
    notes.append("Lift consistently and aim for 7-9 h of sleep - the surplus only turns into muscle with training.")
    return notes


def render(date, profile, T, plans, missing, extra=(), places=None):
    places = places or {}
    place = " + ".join(dict.fromkeys(places.values())) or "Nelson Court"
    L = []
    L.append(f"# {place} meal plan - {date:%A, %B %d, %Y}")
    L.append("")
    L.append(f"Goal: maingain + overall health | {profile['weight_lb']} lb, {profile['activity']} activity")
    adj = f" {T['kcal_adjust']:+d} trend adjustment" if T.get("kcal_adjust") else ""
    L.append(f"Daily targets: {T['kcal']:.0f} kcal (maintenance ~{T['tdee']:.0f} + {profile['surplus_kcal']} surplus{adj}), "
             f"protein {T['protein']:.0f} g, carbs {T['carbs']:.0f} g, fat {T['fat']:.0f} g, "
             f"fiber >= {T['fiber']:.0f} g, sodium <= {T['sodium']:.0f} mg")
    L.append("")
    day = dict.fromkeys(NUTRS, 0.0)
    for meal, (sel, by_key, mt, backups) in plans.items():
        at = places.get(meal, place)
        L.append(f"## {meal.title()} at {at}  (aim ~{mt['kcal']:.0f} kcal, {mt['protein']:.0f} g protein)")
        L.append("")
        if not sel:
            L.append(f"_{at} isn't serving this meal today (or nothing fit your filters)._")
            L.append("")
            continue
        stations = {}
        for k, s in sel.items():
            stations.setdefault(by_key[k]["station"], []).append((by_key[k], s))
        for st, rows in stations.items():
            L.append(f"**{st}**")
            for it, s in rows:
                L.append(f"- {it['name']} - {portion_text(it['n']['serving'], s)}")
                L.append(f"  `{fmt_row(it['n'], s)}`")
            L.append("")
        t = totals(sel, by_key)
        for f in NUTRS:
            day[f] += t[f]
        L.append(f"Meal total: `{fmt_row(t)}`")
        if backups:
            L.append("")
            L.append("If a main is gone, swap in: " + "; ".join(
                f"{b['name']} ({b['station']}, {b['n']['protein']:.0f} g protein / {b['n']['kcal']:.0f} kcal per {b['n']['serving']})"
                for b in backups))
        L.append("")
    L.append("## Day total vs target")
    L.append("")
    L.append("| | Planned | Target |")
    L.append("|---|---:|---:|")
    rows = [("Calories", "kcal", ""), ("Protein", "protein", " g"), ("Carbs", "carbs", " g"), ("Fat", "fat", " g"),
            ("Fiber", "fiber", " g"), ("Sodium", "sodium", " mg"), ("Saturated fat", "sat_fat", " g"),
            ("Added sugar", "added_sugar", " g"), ("Fruit & veg", "produce_g", " g")]
    for label, f, u in rows:
        cmp = "<= " if f in ("sodium", "sat_fat", "added_sugar") else (">= " if f in ("fiber", "produce_g") else "")
        L.append(f"| {label} | {day[f]:.0f}{u} | {cmp}{T[f]:.0f}{u} |")
    L.append("")
    notes = list(extra) + day_notes(T, day)
    L.append("## Notes")
    L.append("")
    L.extend(f"- {n}" for n in notes)
    if missing:
        L.append(f"- No nutrition label available for: {', '.join(sorted(set(missing)))} (skipped).")
    L.append("")
    L.append("_Nutrition values come from Ohio University Culinary Services labels and are approximations; "
             "menus can change without notice._")
    return "\n".join(L) + "\n"

def plan_to_dict(date, profile, T, plans, missing, extra=(), places=None):
    places = places or {}
    place = " + ".join(dict.fromkeys(places.values())) or "Nelson Court"
    r1 = lambda x: round(x, 1)
    nut = lambda n, s=1.0: {f: r1(n[f] * s) for f in NUTRS}
    out_meals, day = [], dict.fromkeys(NUTRS, 0.0)
    for meal, (sel, by_key, mt, backups) in plans.items():
        items = []
        for k, s in sel.items():
            it = by_key[k]
            items.append({"id": k, "name": it["name"], "station": it["station"], "servings": s,
                          "serving": it["n"]["serving"], "portion": portion_text(it["n"]["serving"], s),
                          "tags": it["tags"], "dessert": it["dessert"], **nut(it["n"], s)})
        t = totals(sel, by_key) if sel else dict.fromkeys(NUTRS, 0.0)
        for f in NUTRS:
            day[f] += t[f]
        out_meals.append({
            "meal": meal, "place": places.get(meal, place), "served": bool(sel),
            "target": {"kcal": round(mt["kcal"]), "protein": round(mt["protein"])},
            "items": items, "totals": nut(t),
            "backups": [{"name": b["name"], "station": b["station"], "serving": b["n"]["serving"],
                         "kcal": r1(b["n"]["kcal"]), "protein": r1(b["n"]["protein"])} for b in backups],
        })
    return {
        "date": date.isoformat(),
        "place": place,
        "label": f"{date:%A, %B} {date.day}",
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "profile": {k: profile[k] for k in ("weight_lb", "height_in", "age", "activity", "surplus_kcal")},
        "targets": {k: round(v) for k, v in T.items()},
        "meals": out_meals,
        "totals": nut(day),
        "notes": list(extra) + day_notes(T, day),
        "missing": sorted(set(missing)),
    }


# --------------------------------------------------------------------------- profile

def load_profile():
    env = os.environ.get("NELSON_PROFILE", "").strip()
    if env:
        if not env.startswith("{"):
            # Stored base64-encoded so web forms can't mangle the JSON's quotes.
            import base64
            env = base64.b64decode(env).decode("utf-8")
        return {**DEFAULT_PROFILE, **json.loads(env)}, False
    if PROFILE_PATH.exists():
        p = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return {**DEFAULT_PROFILE, **p}, False
    PROFILE_PATH.write_text(json.dumps(DEFAULT_PROFILE, indent=2), encoding="utf-8")
    return dict(DEFAULT_PROFILE), True


def setup_profile():
    p, _ = load_profile()

    def ask(label, key, cast):
        raw = input(f"{label} [{p[key]}]: ").strip()
        if raw:
            p[key] = cast(raw)

    print("Enter your stats (press Enter to keep the value in brackets).")
    ask("Body weight (lb)", "weight_lb", float)
    ask("Height (inches, e.g. 70 for 5'10\")", "height_in", float)
    ask("Age", "age", int)
    ask("Sex for the BMR formula (male/female)", "sex", str)
    ask("Activity (sedentary/light/moderate/very/athlete)", "activity", str)
    ask("Daily surplus over maintenance (kcal)", "surplus_kcal", float)
    ask("Allergens to avoid, comma list of P TN SH F M E S SS (blank = none)", "avoid_allergens",
        lambda s: [x.strip().upper() for x in s.split(",") if x.strip()])
    ask("Foods/words to avoid, comma list (blank = none)", "avoid_keywords",
        lambda s: [x.strip() for x in s.split(",") if x.strip()])
    ask("Meals you eat at Nelson, comma list (breakfast,lunch,dinner)", "meals",
        lambda s: [x.strip().lower() for x in s.split(",") if x.strip()])
    PROFILE_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")
    print(f"Saved to {PROFILE_PATH}")

# --------------------------------------------------------------------------- weight trend

def load_weights():
    try:
        st = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("log", {})
    st.setdefault("kcal_adjust", 0)
    st.setdefault("last_adjust", None)
    st.setdefault("history", [])
    return st


def save_weights(st):
    WEIGHTS_PATH.write_text(json.dumps(st, indent=1), encoding="utf-8")


def window_avg(log, start, end):
    vals = [w for d, w in log.items() if start <= dt.date.fromisoformat(d) <= end]
    return sum(vals) / len(vals) if len(vals) >= 4 else None


def weight_trend(st, today, profile):
    """7-day average this week vs last week (lb/week), and the target band.
    Target: gain 0.25-0.35% of body weight per week (Helms 2023, Iraki 2019)."""
    log = st["log"]
    this_wk = window_avg(log, today - dt.timedelta(6), today)
    last_wk = window_avg(log, today - dt.timedelta(13), today - dt.timedelta(7))
    bw = this_wk or profile["weight_lb"]
    lo = bw * profile.get("gain_pct_low", 0.25) / 100
    hi = bw * profile.get("gain_pct_high", 0.35) / 100
    rate = (this_wk - last_wk) if (this_wk and last_wk) else None
    return rate, lo, hi, this_wk


def update_calorie_adjustment(st, today, profile):
    """Every 2 weeks, nudge calories toward the target rate of gain. Ignores the first
    2 weeks of logging (water/glycogen noise). Step sizes are practitioner convention."""
    log = st["log"]
    if not log:
        return None
    first = min(dt.date.fromisoformat(d) for d in log)
    if today - dt.timedelta(13) < first + dt.timedelta(14):
        return None
    if st["last_adjust"] and (today - dt.date.fromisoformat(st["last_adjust"])).days < 14:
        return None
    rate, lo, hi, _ = weight_trend(st, today, profile)
    if rate is None:
        return None
    if rate > hi + 0.22:        # more than ~0.1 kg/week above target
        step = -150
    elif rate > hi:
        step = -100
    elif rate < 0:
        step = 200
    elif rate < lo:
        step = 100
    else:
        step = 0
    st["last_adjust"] = today.isoformat()
    st["kcal_adjust"] += step
    st["history"].append({"date": today.isoformat(), "rate_lb_wk": round(rate, 2), "step": step,
                          "total_adjust": st["kcal_adjust"]})
    save_weights(st)
    return step


def weight_status(st, today, profile):
    log = st["log"]
    rate, lo, hi, avg = weight_trend(st, today, profile)
    adj = st["kcal_adjust"]
    adj_txt = f" Calories adjusted {adj:+d} kcal so far from your trend." if adj else ""
    if not log:
        return ("Log your morning weight so the plan can tune your calories: "
                "python nelson_bot.py --weigh 170.4 (auto-adjusting starts after 4 weeks of logs).")
    if rate is None:
        return (f"Weight log: {len(log)} day(s). Keep logging each morning; the trend needs "
                f"2 full weeks of weigh-ins, and calories start auto-adjusting after 4 weeks.{adj_txt}")
    verdict = "on target" if lo <= rate <= hi else ("faster than target" if rate > hi else "slower than target")
    return (f"Weight trend: {rate:+.2f} lb/week (7-day avg {avg:.1f} lb; target +{lo:.2f} to +{hi:.2f}), "
            f"{verdict}.{adj_txt}")


# --------------------------------------------------------------------------- prefetch

def prefetch(start, days, profile):
    """Fill the menu + label caches for several days, patiently (no 2-minute limit)."""
    jobs = [(start + dt.timedelta(d), m) for d in range(days) for m in MEAL_IDS]
    print(f"Prefetching {len(jobs)} menus ({days} day(s)); ohio.edu is slow, this can take a while ...",
          file=sys.stderr)
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda j: (j, fetch_menu(j[0], j[1], True, location_for(j[0], profile, None, j[1]))), jobs))
    for (d, m), items in results:
        print(f"  {d} {m}: {len(items)} items" if items else f"  {d} {m}: not available", file=sys.stderr)
    all_items = [it for _, items in results for it in items]
    attach_nutrition(all_items, profile)
    have = sum(1 for it in all_items if it["n"])
    print(f"Done: labels cached for {have}/{len(all_items)} menu entries "
          f"(the rest are dressings/desserts/etc. the planner skips, or failed to load).", file=sys.stderr)

# --------------------------------------------------------------------------- publish (GitHub Pages)

def short_items(meal):
    """'Shwarma Chicken 8 oz, Spaghetti Noodles 12 oz, ...' for the widget."""
    parts = []
    for it in meal["items"]:
        portion = it["portion"].split(" (")[0]
        parts.append(f"{it['name']} {portion}")
    return ", ".join(parts)


def publish(plan, out_dir):
    """Write the public copy of a plan for the website and widget. Personal stats
    (weight, height, age) are left out because the repository is public."""
    out = Path(out_dir)
    (out / "plans").mkdir(parents=True, exist_ok=True)
    public = {k: v for k, v in plan.items() if k != "profile"}
    public["notes"] = [n for n in plan.get("notes", []) if not n.startswith(("Weight", "Log your morning"))]
    text = json.dumps(public, indent=1)
    (out / "plans" / f"{plan['date']}.json").write_text(text, encoding="utf-8")
    (out / "latest.json").write_text(text, encoding="utf-8")
    dates = sorted((f.stem for f in (out / "plans").glob("????-??-??.json")), reverse=True)
    (out / "plans" / "index.json").write_text(json.dumps({"dates": dates[:30]}), encoding="utf-8")
    meals = {m["meal"]: m for m in plan["meals"]}
    widget = {
        "date": plan["date"],
        "label": plan["label"],
        "place": plan.get("place", ""),
        "kcal": round(plan["totals"]["kcal"]),
        "protein": round(plan["totals"]["protein"]),
        "target_kcal": plan["targets"]["kcal"],
        "target_protein": plan["targets"]["protein"],
        "updated": plan["generated"],
    }
    for name in ("breakfast", "lunch", "dinner"):
        m = meals.get(name)
        if m and m["items"]:
            widget[name] = short_items(m)
            widget[name + "_place"] = m.get("place", "")
            widget[name + "_kcal"] = round(m["totals"]["kcal"])
            widget[name + "_protein"] = round(m["totals"]["protein"])
    (out / "widget.json").write_text(json.dumps(widget, indent=1), encoding="utf-8")
    print(f"Published {plan['date']} to {out}", file=sys.stderr)


# --------------------------------------------------------------------------- main

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Build a maingain meal plan from today's Nelson Court menu.")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--meals", help="comma list, e.g. lunch,dinner (default: from profile)")
    ap.add_argument("--setup", action="store_true", help="enter your body stats and preferences")
    ap.add_argument("--no-cache", action="store_true", help="re-download every nutrition label")
    ap.add_argument("--allow-dessert", action="store_true")
    ap.add_argument("--time-limit", type=float, default=None,
                    help="max seconds to spend downloading from ohio.edu (default 110)")
    ap.add_argument("--prefetch", type=int, metavar="DAYS", nargs="?", const=2,
                    help="slowly download menus + labels for the next DAYS days (default 2) into the "
                         "cache, so normal runs take seconds. Good to schedule overnight.")
    ap.add_argument("--patient", action="store_true",
                    help="for the scheduled daily run: wait up to 25 minutes for the slow OU site, then "
                         "build the plan. Ends with a RESULT: line and a non-zero exit code on failure.")
    ap.add_argument("--location", help='dining hall for this run, e.g. "THE DISTRICT ON WEST GREEN" '
                                       "(default: from profile, by weekday)")
    ap.add_argument("--publish-dir", metavar="DIR",
                    help="on success, also write the public plan, plan list and widget.json into DIR "
                         "(the GitHub Pages folder)")
    ap.add_argument("--weigh", type=float, metavar="LB",
                    help="log this morning's body weight (lb) for --date/today, show the trend, and exit")
    ap.add_argument("--trend", action="store_true", help="show your weight trend and calorie adjustment, then exit")
    args = ap.parse_args()
    global TIME_LIMIT, REQUEST_TIMEOUT
    TIME_LIMIT = args.time_limit or 110.0
    if args.prefetch:
        TIME_LIMIT, REQUEST_TIMEOUT = 3600.0, 300.0
    elif args.patient:
        TIME_LIMIT, REQUEST_TIMEOUT = (args.time_limit or 1500.0), 300.0

    if args.setup:
        setup_profile()
        return
    profile, created = load_profile()
    if created:
        print(f"NOTE: created {PROFILE_PATH.name} with default stats (160 lb, 5'10\", 19). "
              f"Run `python nelson_bot.py --setup` to use your real numbers.\n", file=sys.stderr)
    if args.allow_dessert:
        profile["allow_dessert"] = True
    date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    if args.prefetch:
        prefetch(date, args.prefetch, profile)
        return
    weights = load_weights()
    if args.weigh or args.trend:
        if args.weigh:
            weights["log"][date.isoformat()] = args.weigh
            save_weights(weights)
            print(f"Logged {args.weigh} lb for {date}.")
        step = update_calorie_adjustment(weights, date, profile)
        if step:
            print(f"Calories changed {step:+d} kcal based on your 2-week trend.")
        print(weight_status(weights, date, profile))
        return
    meals = [m.strip().lower() for m in (args.meals.split(",") if args.meals else profile["meals"])]
    meals = [m for m in ("breakfast", "lunch", "dinner") if m in meals]

    locations = {m: location_for(date, profile, args.location, m) for m in meals}
    places = {m: place_name(loc) for m, loc in locations.items()}
    place = " + ".join(dict.fromkeys(places.values()))
    print(f"Loading menus for {date}: " + ", ".join(f"{m} at {places[m]}" for m in meals) + " ...", file=sys.stderr)
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        menus = dict(zip(meals, ex.map(lambda m: fetch_menu(date, m, not args.no_cache, locations[m]), meals)))
    all_items = [it for v in menus.values() for it in v]
    if not all_items:
        print(f"No {place} menu came back for {date}: it may be closed, the menu may not be "
              f"posted yet, or ohio.edu is having trouble. Try again in a few minutes.")
        print("RESULT: NO_MENU", file=sys.stderr)
        sys.exit(2)
    attach_nutrition(all_items, profile, use_cache=not args.no_cache)
    missing = [it["name"] for it in all_items if not it["n"] and name_allowed(it, profile)]
    for it in all_items:
        if it["n"]:
            classify(it, profile)

    step = update_calorie_adjustment(weights, date, profile)
    T = compute_targets(profile, weights["kcal_adjust"])
    extra = [weight_status(weights, date, profile)]
    if step:
        extra.insert(0, f"Calories changed {step:+d} kcal today based on your 2-week weight trend.")
    served = [m for m in meals if menus[m]]
    # Each meal gets its share of the day. If Nelson isn't serving one of the meals you
    # normally eat there, its share moves to the other planned meals. Meals left out with
    # --meals keep their share (you're eating somewhere else).
    split = {m: profile["meal_split"].get(m, 1 / 3) for m in served}
    eaten_elsewhere = [m for m in profile["meals"] if m not in meals]
    total_split = (sum(split.values()) + sum(profile["meal_split"].get(m, 1 / 3) for m in eaten_elsewhere)) or 1

    labelled = [it for it in all_items if it.get("allowed") is not None]
    candidates = [it for it in all_items if name_allowed(it, profile)]
    coverage = len(labelled) / max(1, len(candidates))
    if coverage < 0.7:
        print(f"WARNING: only {coverage:.0%} of usable menu items have nutrition data (ohio.edu returned "
              f"blank labels). This plan is limited - run `python nelson_bot.py --prefetch` and try again.",
              file=sys.stderr)
    rng = random.Random(date.toordinal())
    used_today, plans = set(), {}
    for m in meals:
        if m not in served:
            plans[m] = ({}, {}, {"kcal": 0, "protein": 0}, [])
            continue
        frac = split[m] / total_split
        mt = {k: (v * frac if k in SCALED else v) for k, v in T.items()}
        mt["_dessert_max"] = profile.get("dessert_max_kcal", 300)
        # Desserts (if allowed) go in the day's last meal, up to desserts_per_day.
        mt["_dessert"] = bool(profile.get("allow_dessert") and profile.get("desserts_per_day", 1) > 0
                              and m == served[-1])
        sel, by_key = plan_meal(menus[m], mt, used_today, rng)
        used_today |= set(sel)
        backups = []
        if by_key:
            backups = sorted((i for i in by_key.values() if i["protein_src"] and i["key"] not in sel),
                             key=protein_rank, reverse=True)[:3]
        plans[m] = (sel, by_key, mt, backups)

    if not any(by_key[k]["fish"] for sel, by_key, _, _ in plans.values() for k in sel):
        extra.append("No fish in today's plan. Aim for 2 non-fried fish meals a week (salmon is ideal).")
    out = render(date, profile, T, plans, missing, extra, places)
    PLANS_DIR.mkdir(exist_ok=True)
    path = PLANS_DIR / f"{date.isoformat()}.md"
    path.write_text(out, encoding="utf-8")
    # Same plan as JSON, for the phone page (one document per day).
    (PLANS_DIR / f"{date.isoformat()}.json").write_text(
        json.dumps(plan_to_dict(date, profile, T, plans, missing, extra, places), indent=1), encoding="utf-8")
    print(out)
    print(f"(saved to {path})", file=sys.stderr)
    planned = [m for m, (sel, _, _, _) in plans.items() if sel]
    if coverage < 0.7 or len(planned) < len(meals):
        # Incomplete data: the plan file exists, but it should not replace a good plan on the phone page.
        print(f"RESULT: LIMITED coverage={coverage:.0%} meals_planned={','.join(planned) or 'none'}", file=sys.stderr)
        sys.exit(3)
    if args.publish_dir:
        publish(json.loads((PLANS_DIR / f"{date.isoformat()}.json").read_text(encoding="utf-8")), args.publish_dir)
    print(f"RESULT: OK {PLANS_DIR / (date.isoformat() + '.json')}", file=sys.stderr)


if __name__ == "__main__":
    main()
