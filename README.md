# Nelson Court meal-plan bot

Reads the Nelson Court menu from Ohio University's dining website (ohio.edu/food/menus),
pulls each item's nutrition label and builds a breakfast / lunch / dinner plan for a
**maingain**: about 200 kcal over maintenance, roughly 0.9 g of protein per lb of body weight,
plenty of fiber and veg, and limits on sodium, saturated fat, added sugar, fried food and processed meat.

It needs only Python 3, with no extra packages to install.

## First time

```
python nelson_bot.py --setup
```
It asks for your weight, height, age, activity level, allergens and foods to avoid, and which meals you
eat at Nelson. These go into `profile.json`, which you can also edit by hand.

## Every day

```
python nelson_bot.py                    # today's plan
python nelson_bot.py --date 2026-11-10  # another day
python nelson_bot.py --meals lunch,dinner
```
The plan prints to the screen and is saved to `plans/<date>.md`.

## Daily update, website and widget (runs on GitHub)

A GitHub Actions workflow (`.github/workflows/daily-plan.yml`) runs the bot at 6 AM Eastern, with a
retry at 11 AM in case OU's menu site is down. It publishes the plan to `docs/`, which GitHub Pages
serves as a phone-friendly website. It also writes `docs/widget.json`, a short summary for the Android
home-screen widget (see `WIDGET.md`). No computer has to be on.

- Your body stats are not in this repository. The workflow reads them from a repository secret
  named `NELSON_PROFILE` (the contents of your local `profile.json`).
- The published plans leave out your weight, height and age.
- If Nelson is closed or OU's site returns no usable menu, nothing is published that day, and the
  website shows an "Older plan" warning above the last plan.
- To run it by hand: the repository's Actions tab > Daily meal plan > Run workflow.

## Weigh-ins (calories tune themselves)

The calorie formula is only a starting guess, often off by a few hundred calories for lifters. Weigh
yourself each morning (after the bathroom, before eating) and log it on your computer (the GitHub
version does not track weigh-ins yet):

```
python nelson_bot.py --weigh 165.0
python nelson_bot.py --trend
```

The first two weeks are ignored because water and glycogen add noise. After that, the bot compares this week's
7-day average with last week's every 2 weeks and adjusts calories toward a gain of 0.25-0.35% of body
weight per week:
- too fast: -100 to -150 kcal
- too slow: +100 kcal
- losing weight: +200 kcal

The research behind every target is summarized in a report kept on the owner's computer (not in this repository).

## Why it can be slow, and how to make it fast

The OU menu site is slow and unreliable: one page can take anywhere from 10 seconds to more than
4 minutes, and sometimes it comes back empty. The bot handles this in three ways:

- **Cache.** Menus and nutrition labels are saved in `.cache/`. Most Nelson items repeat from day to
  day, so once they're cached a plan builds in a few seconds.
- **Time limit.** A normal run never spends more than 110 s on downloads (`--time-limit` changes this).
  It builds the best plan it can from whatever arrived in that time.
- **Prefetch.** `python nelson_bot.py --prefetch 2` slowly downloads today's and tomorrow's menus
  and labels with no time limit. Run it the night before, or schedule it, and your morning run takes
  seconds.

## Tweaks in profile.json

| key | meaning |
|---|---|
| `surplus_kcal` | calories over maintenance (default 250; the research supports 200 to 300 for trained lifters) |
| `gain_pct_low` / `gain_pct_high` | target weekly gain as % of body weight (0.25 to 0.35) |
| `produce_target_g` | fruit and vegetable grams per day (default 600) |
| `dessert_max_kcal` | largest dessert the plan will pick (default 300) |
| `protein_g_per_lb` | protein target per lb of body weight |
| `meal_split` | share of the day's calories for each meal |
| `avoid_allergens` | allergen codes from the menu: P TN SH F M E S SS |
| `avoid_keywords` | words to avoid in item names, e.g. `["pork", "mushroom"]` |
| `vegetarian` / `vegan` | uses the menu's VT / VN tags |
| `location` / `locations_by_weekday` | which dining hall to plan for, using OU's names from ohio.edu/food/menus. Default is `NELSON COURT`. A weekday can name one hall for the whole day or one per meal: Tuesday lunch is set to `THE DISTRICT ON WEST GREEN` (the hall in Boyd), with dinner at Nelson. `--location "..."` overrides every meal for one run |
| `favorites` | words for foods you like (e.g. `"chicken"`, `"penne"`, `"pizza"`): favored, can appear at both lunch and dinner, and up to 3 portions of a favorite protein |
| `allow_dessert` / `desserts_per_day` | adds one full dessert to your last meal of the day |

Nutrition numbers are Culinary Services' approximations, and menus can change without notice.
