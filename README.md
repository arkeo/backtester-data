# Market history

Published market history for the Backtester application, refreshed
automatically every three hours by GitHub Actions.

The files under [Releases](../../releases/tag/history) are sealed: they are not
archives, they are named by hash, and they open only in the application. There
is nothing here to download and use directly.

## Setting it up

Under **Settings -> Secrets and variables -> Actions**:

| kind | name | value |
|---|---|---|
| secret | `BACKTESTER_KEY` | the publisher key the application is built with |
| variable | `SYMBOLS` | `all` |
| variable | `MINUTES` | `150` |

Every instrument is fetched back as far as its feed carries it — the year 2000
for the currency pairs — so the first runs have a lot to do. Each run publishes
what it managed and the next one carries on.

Then **Actions -> publish history -> Run workflow** for the first run. After
that it runs itself.

The address to give the application is:

    https://github.com/OWNER/REPO/releases/download/history

## How a run works

One instrument at a time. A run restores that instrument from what is already
published, fetches whatever is missing, seals it, uploads it, and deletes it
before moving to the next.

Nothing is kept between runs, because **the release is the saved state** — each
bundle carries its own record of what it contains, so the next run knows where
to continue. That is what makes the whole catalogue possible on a free runner:
peak disk is one instrument rather than the sixteen gigabytes a full catalogue
comes to, which fits neither the runner's disk nor its cache.

A run has a time limit and takes the instruments that have gone longest without
a refresh, so being cut short leaves the mirror better than it found it rather
than half-writing something. An instrument whose backfill stops making progress
is recorded as stalled instead of being retried from the start every three
hours.

## What is not here

The cryptocurrency pairs are not published. There are several thousand of them
and the application fetches whichever one you pick directly, in seconds — which
is cheaper for everyone than mirroring them all.
