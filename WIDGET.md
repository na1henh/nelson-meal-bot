# Android home-screen widget (KWGT)

The daily workflow publishes a small summary file:

    https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json

Open that link on your phone first. If you see text starting with `{"date":`, the widget will work.
Replace `YOUR-USERNAME` and `YOUR-REPO` everywhere below with your own.

## 1. Install

1. Install **KWGT Kustom Widget Maker** from the Play Store (free; the Pro key only removes the
   "can't save" limit on some presets, which this widget doesn't need).
2. Long-press your home screen > Widgets > KWGT > drag a **4x2** (or 4x3) widget onto the screen.
3. Tap the empty widget to open the KWGT editor, then pick **Create new** (blank).

## 2. Add the text lines

In the editor, tap **+** > **Text** for each line below. For each one, tap the text item, tap the
**Text** field, then the **calculator icon** (formula), and paste the formula. Change font size and
colour to taste.

| Line | Formula |
|---|---|
| Title | `$wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".label")$` |
| Totals | `$wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".kcal")$ kcal · $wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".protein")$ g protein` |
| Lunch heading | `Lunch · $wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".lunch_place")$` |
| Lunch items | `$wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".lunch")$` |
| Dinner heading | `Dinner · $wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".dinner_place")$` |
| Dinner items | `$wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".dinner")$` |

Set the item lines to **multiple lines** (Text > Lines > a number like 3) so long lists wrap.

Optional extras the file also has: `.target_kcal`, `.target_protein`, `.lunch_kcal`,
`.lunch_protein`, `.dinner_kcal`, `.dinner_protein`, `.date`, `.updated`.

## 3. Show a warning when the plan is old

Add one more text line with this formula. It shows nothing on a normal day and a warning when the
newest plan isn't today's (for example when OU's menu site was down):

    $if(wg("https://YOUR-USERNAME.github.io/YOUR-REPO/widget.json", json, ".date") != df("yyyy-MM-dd"), "Older plan - today's isn't out yet", "")$

## 4. Tap to open the full plan

Select the widget's root layer > **Touch** > **+** > Action: **Open link** > paste
`https://YOUR-USERNAME.github.io/YOUR-REPO/` . Tapping the widget then opens the full page, where you
can tick off foods as you eat them.

## 5. Save and refresh

Tap the save icon (floppy disk) at the top right. KWGT re-downloads web data on its own schedule
(about every 30-60 minutes by default). To change it: KWGT app > Settings > **Update interval**.

## Troubleshooting

- **Widget shows nothing:** open the widget.json link in your phone's browser. If it doesn't load,
  GitHub Pages isn't on yet, or no plan has been published.
- **Shows yesterday's food:** the morning update hasn't finished yet, or OU's site was down (the
  11 AM retry may still fix it).
- **Battery saver:** some phones stop KWGT updating in the background. Set KWGT's battery usage to
  "Unrestricted" in Android settings.
