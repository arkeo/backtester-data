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
| variable | `SYMBOLS` | `all` |
| variable | `MINUTES` | `150` |

Every instrument is fetched back as far as its source carries it — the year
2000 for the currency pairs — so the first runs have a lot to do. Each run
publishes what it managed and the next one carries on.

Then **Actions -> publish history -> Run workflow** for the first run. After
that it runs itself every three hours.

The address to give the application is:

    https://github.com/OWNER/REPO/releases/download/history
