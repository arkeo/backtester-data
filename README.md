# Market history

Published market history for the Backtester application, refreshed
automatically once a day by GitHub Actions.

The files under [Releases](../../releases/tag/history) are sealed: they are not
archives, they are named by hash, and they open only in the application. There
is nothing here to download and use directly.

The data itself comes from the public feeds published by HistData, Dukascopy
and Binance.

## Setting it up

Under **Settings -> Secrets and variables -> Actions**:

| kind | name | value |
|---|---|---|
| secret | `BACKTESTER_KEY` | the publisher key the application is built with |
| secret | `BACKTESTER_CONTENT_KEYS` | the contents of `CONTENT-KEYS.json` |
| variable | `SYMBOLS` | `all` |
| variable | `MINUTES` | `150` |

`BACKTESTER_CONTENT_KEYS` is what makes a licence worth buying. Without it the
job seals everything with `BACKTESTER_KEY`, which is compiled into every copy
of the application — so the whole archive opens whether anyone has paid or not.

It must contain a key for the **current month**. If it does not, the job stops
with a message instead of falling back, because falling back would give the
catalogue away and would look exactly like a run that worked. Add next month's
with `python installer/make_licence_keys.py month`, then update the secret. Two
months ahead is a comfortable margin; one is not, because the changeover is at
midnight UTC on the first.

Every instrument is fetched back as far as its source carries it — the year
2000 for the currency pairs — so the first runs have a lot to do. Each run
publishes what it managed and the next one carries on.

Then **Actions -> publish history -> Run workflow** for the first run. After
that it runs itself every three hours.

The address to give the application is:

    https://github.com/OWNER/REPO/releases/download/history
