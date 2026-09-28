# Offline portfolio demo

Run from the repository root after installing the runtime dependencies:

```powershell
python -m pip install -r ultimate-bot/requirements.txt
python -X utf8 demo/run_demo.py
```

The command analyzes fictional subtitle examples with the project's real parser and salary rules, then renders NAS, PP, and LK rows using the same worksheet row builder as the bot. It makes no Telegram or Google requests and needs no bot token, service-account key, or spreadsheet ID.

The examples demonstrate a movie review fee, a first-episode review fee in season four, PP's no-review-fee behavior, and LK's count-only layout. All titles and lines are synthetic. The printed rows are a local preview, not a link to or copy of a live salary sheet.

## Example result

| Destination | Files | Lines | Base pay | Review fees | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| NAS | 2 | 5 | 115 Ks | 3,000 Ks | 3,115 Ks |
| PP | 1 | 2 | 40 Ks | 0 Ks | 40 Ks |
| LK | 1 | 2 | — | — | Count only |

The NAS examples include a two-line first-part movie at 20 Ks/line and a three-line first episode at 25 Ks/line. Each receives a 1,500 Ks review fee. PP never receives a review fee; LK does not calculate money.
