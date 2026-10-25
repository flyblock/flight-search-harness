# Flight Search Harness

**Let any AI agent hunt for genuinely cheap flights, and keep score while it does.**

Flight Search Harness is a small standard-library Python ledger plus three Skills. Your AI client does the searching with its own browser or APIs; the harness saves every price it sees, compares the options under your rules, and keeps pushing the agent toward the cheaper routes it would not think of by itself. It works with Codex, Claude, DeepSeek, your own model, or a person typing JSON.

## What makes it different

- **Nothing found is lost.** Every fare, partial price and lead is saved the moment it is seen. Missing links, odd timestamps or a seller name that differs from the website never cause a price to be discarded.
- **The best fare is a target, not a finish line.** The agent challenges the current best with other dates, airports, sellers and ticket structures. A beaten fare stays as a runner-up, so you get a ranked shortlist for free.
- **It looks past borders.** For a city or region, the agent is asked to check nearby, cross-border, secondary and hub airports (Frankfurt → Luxembourg, Brussels, Zurich; New York → Philadelphia) and gets told how cheap a fare from each must be after getting there.
- **It searches from both ends.** Priced pieces become budgets: "reach Urumqi from Frankfurt for under ¥1,820", "connect Almaty → Urumqi by any mode for under ¥840". That is how routes like *Frankfurt → Almaty (low-cost) → bus across the Khorgos border → train → domestic flight to Hong Kong* become findable.
- **Random loops are first-class.** Multi-leg loops from a set of nearby airports are ranked by distance per unit of currency, with your hard limits (legs, total hours, minimum km per currency) checked by code, and hints listing the legs that could improve the loop.
- **Honest answers.** Results state how far each price was verified (listing, selected itinerary, fare review, checkout), self-transfer and visa risks, and what was not searched. Nothing claims to be the global cheapest.
- **Small output for small context windows.** Commands return compact JSON; a Run with thousands of observations still answers in a few kilobytes.

## Three Skills

| Skill | Use it for | Default goal |
|---|---|---|
| `low-cost-round-trip-flight-search` | Outbound and return to a chosen destination | Lowest total, with stay-length rules |
| `low-cost-one-way-travel-flight-search` | One way to a chosen destination | Lowest total, including bus/rail pieces when allowed |
| `low-cost-random-loop-flight-search` | Open-destination loops back to your accepted airports | Most kilometres per unit of currency |

## Quick start

**Install a Skill from [skills.sh](https://skills.sh/flyblock/flight-search-harness):**

```bash
npx skills add flyblock/flight-search-harness --skill low-cost-round-trip-flight-search
```

On first use the Skill downloads the matching framework release from this repository into `~/.flight-search-harness` (it says so) and reuses it afterwards. Then just ask your AI, for example:

> Find the cheapest round trip from Frankfurt city to any Greater Bay Area airport for 1 adult, both flights before 30 November, staying 7–14 nights, priced in CNY. Overnight and self-transfer connections are fine.

> Plan a random loop from budget airports around Frankfurt and back to any of them: at least 4 flights, all within 48 hours, at least 5 km per CNY.

**Or use the whole repository** (Python 3.10+):

```bash
git clone https://github.com/flyblock/flight-search-harness.git
```

`python -m pip install -r requirements.txt` adds the IANA timezone data (`tzdata`) on systems that lack it; without it, time-zone-dependent checks stay "unknown" rather than guessed.

**Any other AI client:** start the conversation with "Read `<repo>/skills/low-cost-round-trip-flight-search/SKILL.md` and follow it for my request: …". The client needs to run shell commands with Python, read web pages or travel APIs, and write local files. Nothing else is required.

## What you get

A ranked comparison built from what was actually observed, for example (live test, 25 Oct 2026; prices change):

| Rank | One way Frankfurt city → Greater Bay Area | Total (CNY) | Verified to |
|---|---|---|---|
| 1 | FlixBus Frankfurt → Brussels, then HU492 + HU7803 Brussels → Beijing → Guangzhou | 2,221 incl. bus and transit | seller passenger page |
| 2 | CA932 + CA115 Frankfurt → Beijing → Hong Kong | 2,262 incl. airport train | fare review |

Each line comes with dates, flight numbers, baggage and fare rules, connection times, and disclosures such as "separate tickets: a missed connection is at your risk".

## How it works

```text
Request ─► search a source ─► record (query + observations) ─► feedback hints
   ▲                                                              │
   └── challenge / discover airports / combine pieces ◄───────────┘
                                   │
                                deliver ─► ranked answer with disclosures
```

The agent decides what to search; code saves, deduplicates queries, checks the user's rules, ranks, and computes the hints. Main commands: `start`, `record`, `combine`, `revise`, `deliver`, `status`, `clock`. The full reference is [docs/search-workflow.md](docs/search-workflow.md); fill-in examples are in [examples/](examples).

Ranking counts what can change the choice: tickets, mandatory bus/rail legs between flights, and access to airports the agent discovered outside your own. City-to-airport access that is the same for every option is left out unless you ask for a door-to-door total.

## Data and privacy

Everything stays on your machine. Runs are JSON files under the data directory the Skill reports (`~/.flight-search-harness/data` for standalone installs; `runtime_data_root` in [FLIGHT-SEARCH-HARNESS-MANIFEST.json](FLIGHT-SEARCH-HARNESS-MANIFEST.json) for a full checkout). The harness never books, pays, signs in or sends your data anywhere.

## Limits

- It does not search by itself; results are only as broad as the agent's searching.
- Prices are not held. Book from the seller after re-checking.
- Children and infants are not modelled yet (adults only).
- Results describe what was searched, not a guaranteed global optimum.

## Project

- Versions are plain numbers, one per commit, each with a Git tag; see [Releases](https://github.com/flyblock/flight-search-harness/releases).
- Contributors: read [AGENTS.md](AGENTS.md), the [glossary](GLOSSARY.md) and [naming conventions](docs/naming.md).
- License: [Apache-2.0](LICENSE).
