"""The instrument catalog.

One row per tradable symbol, naming where its history comes from and how its
prices are stored. Everything else in the application reads instruments from
here, so adding a market is a matter of adding a line.

Price storage
-------------
Bars are kept as integers: ``stored = round(price / point)``. That is exact,
compact, and free of the float rounding that turns 1.08765 into 1.0876499.
``point`` is the smallest price increment the instrument quotes in, which is
also what MetaTrader calls Point and what stop distances are measured in.

Sources
-------
``histdata``  one HTTP request per YEAR, M1 bars, no spread. Cheapest depth.
``dukascopy`` one HTTP request per DAY, M1 bars, separate bid and ask files so
              the real spread of every bar is known. The only source with Dow.
``binance``   one HTTP request per MONTH, crypto only, genuinely 24/7.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str            # our canonical name, e.g. "EURUSD"
    name: str              # what the user sees
    group: str             # forex / metals / indices / energy / crypto
    point: float           # smallest price increment
    digits: int            # decimals to display
    source: str            # histdata | dukascopy | binance
    code: str              # the identifier that source uses
    first_year: int        # earliest year the source has
    contract: float = 100_000.0   # units per 1.00 lot
    # Instruments that also exist on Dukascopy can be topped up with true
    # bid/ask spread even when their bars come from HistData.
    duka: str = ""
    # Power of ten Dukascopy scales this instrument's integer prices by.
    # 0 means "work it out from the group" (see sources/dukascopy.scale_for).
    duka_scale: int = 0
    quote: str = "USD"     # currency the profit is denominated in


# --- forex -----------------------------------------------------------------
# HistData carries all of these from 2000 (a handful of crosses start later,
# the fetcher discovers that by getting a 404 and moving on).

_FX_MAJOR = [
    ("EURUSD", "Euro / US Dollar"),
    ("GBPUSD", "British Pound / US Dollar"),
    ("USDJPY", "US Dollar / Japanese Yen"),
    ("USDCHF", "US Dollar / Swiss Franc"),
    ("USDCAD", "US Dollar / Canadian Dollar"),
    ("AUDUSD", "Australian Dollar / US Dollar"),
    ("NZDUSD", "New Zealand Dollar / US Dollar"),
]

_FX_CROSS = [
    ("EURGBP", "Euro / British Pound"),
    ("EURJPY", "Euro / Japanese Yen"),
    ("EURCHF", "Euro / Swiss Franc"),
    ("EURAUD", "Euro / Australian Dollar"),
    ("EURCAD", "Euro / Canadian Dollar"),
    ("EURNZD", "Euro / New Zealand Dollar"),
    ("GBPJPY", "British Pound / Japanese Yen"),
    ("GBPCHF", "British Pound / Swiss Franc"),
    ("GBPAUD", "British Pound / Australian Dollar"),
    ("GBPCAD", "British Pound / Canadian Dollar"),
    ("GBPNZD", "British Pound / New Zealand Dollar"),
    ("AUDJPY", "Australian Dollar / Japanese Yen"),
    ("AUDCHF", "Australian Dollar / Swiss Franc"),
    ("AUDCAD", "Australian Dollar / Canadian Dollar"),
    ("AUDNZD", "Australian Dollar / New Zealand Dollar"),
    ("NZDJPY", "New Zealand Dollar / Japanese Yen"),
    ("NZDCHF", "New Zealand Dollar / Swiss Franc"),
    ("NZDCAD", "New Zealand Dollar / Canadian Dollar"),
    ("CADJPY", "Canadian Dollar / Japanese Yen"),
    ("CADCHF", "Canadian Dollar / Swiss Franc"),
    ("CHFJPY", "Swiss Franc / Japanese Yen"),
]

_FX_EXOTIC = [
    ("USDMXN", "US Dollar / Mexican Peso"),
    ("USDTRY", "US Dollar / Turkish Lira"),
    ("USDZAR", "US Dollar / South African Rand"),
    ("USDSEK", "US Dollar / Swedish Krona"),
    ("USDNOK", "US Dollar / Norwegian Krone"),
    ("USDDKK", "US Dollar / Danish Krone"),
    ("USDPLN", "US Dollar / Polish Zloty"),
    ("USDSGD", "US Dollar / Singapore Dollar"),
    ("USDHKD", "US Dollar / Hong Kong Dollar"),
    ("USDCZK", "US Dollar / Czech Koruna"),
    ("USDHUF", "US Dollar / Hungarian Forint"),
    ("EURTRY", "Euro / Turkish Lira"),
    ("EURSEK", "Euro / Swedish Krona"),
    ("EURNOK", "Euro / Norwegian Krone"),
    ("EURPLN", "Euro / Polish Zloty"),
    ("EURDKK", "Euro / Danish Krone"),
    ("EURCZK", "Euro / Czech Koruna"),
    ("EURHUF", "Euro / Hungarian Forint"),
    ("SGDJPY", "Singapore Dollar / Japanese Yen"),
    ("ZARJPY", "South African Rand / Japanese Yen"),
]


def _fx(pairs, group):
    out = []
    for sym, name in pairs:
        # Yen-quoted pairs (and the forint) quote to 3 decimals, everything
        # else to 5. This mirrors what every 5-digit broker does.
        jpy = sym.endswith("JPY") or sym.endswith("HUF")
        out.append(Instrument(
            symbol=sym, name=name, group=group,
            point=0.001 if jpy else 0.00001,
            digits=3 if jpy else 5,
            source="histdata", code=sym.lower(), first_year=2000,
            contract=100_000.0, duka=sym, quote=sym[3:],
        ))
    return out


INSTRUMENTS = []
INSTRUMENTS += _fx(_FX_MAJOR, "forex")
INSTRUMENTS += _fx(_FX_CROSS, "forex")
INSTRUMENTS += _fx(_FX_EXOTIC, "forex")

# --- metals ----------------------------------------------------------------
INSTRUMENTS += [
    Instrument("XAUUSD", "Gold / US Dollar", "metals", 0.001, 2,
               "histdata", "xauusd", 2009, contract=100.0, duka="XAUUSD"),
    Instrument("XAGUSD", "Silver / US Dollar", "metals", 0.0001, 3,
               "histdata", "xagusd", 2009, contract=5_000.0, duka="XAGUSD"),
]

# --- indices ---------------------------------------------------------------
# The Dow is the one instrument HistData does not carry at all; five candidate
# codes were checked and none exist. It comes from Dukascopy, which since it
# serves one M1 file per day is perfectly affordable.
INSTRUMENTS += [
    Instrument("US30", "Dow Jones 30", "indices", 0.001, 2,
               "dukascopy", "USA30IDXUSD", 2012, contract=1.0, duka="USA30IDXUSD"),
    Instrument("US500", "S&P 500", "indices", 0.001, 2,
               "histdata", "spxusd", 2010, contract=1.0, duka="USA500IDXUSD"),
    Instrument("USTEC", "Nasdaq 100", "indices", 0.001, 2,
               "histdata", "nsxusd", 2010, contract=1.0, duka="USATECHIDXUSD"),
    Instrument("DE40", "DAX 40", "indices", 0.001, 2,
               "histdata", "grxeur", 2010, contract=1.0, duka="DEUIDXEUR", quote="EUR"),
    Instrument("UK100", "FTSE 100", "indices", 0.001, 2,
               "histdata", "ukxgbp", 2010, contract=1.0, duka="GBRIDXGBP", quote="GBP"),
    Instrument("JP225", "Nikkei 225", "indices", 0.001, 2,
               "histdata", "jpxjpy", 2010, contract=1.0, duka="JPNIDXJPY", quote="JPY"),
    Instrument("FR40", "CAC 40", "indices", 0.001, 2,
               "histdata", "frxeur", 2010, contract=1.0, duka="FRAIDXEUR", quote="EUR"),
    Instrument("AU200", "ASX 200", "indices", 0.001, 2,
               "histdata", "auxaud", 2010, contract=1.0, duka="AUSIDXAUD", quote="AUD"),
    Instrument("HK50", "Hang Seng", "indices", 0.001, 2,
               "histdata", "hkxhkd", 2010, contract=1.0, duka="HKGIDXHKD", quote="HKD"),
    Instrument("EU50", "Euro Stoxx 50", "indices", 0.001, 2,
               "histdata", "etxeur", 2010, contract=1.0, duka="EUSIDXEUR", quote="EUR"),
    Instrument("USDX", "US Dollar Index", "indices", 0.001, 3,
               "histdata", "udxusd", 2010, contract=1.0),
]

# --- energy ----------------------------------------------------------------
INSTRUMENTS += [
    Instrument("UKOIL", "Brent Crude Oil", "energy", 0.001, 3,
               "histdata", "bcousd", 2010, contract=1_000.0, duka="BRENTCMDUSD"),
    Instrument("USOIL", "WTI Crude Oil", "energy", 0.001, 3,
               "histdata", "wtiusd", 2010, contract=1_000.0, duka="LIGHTCMDUSD"),
]

# --- crypto ----------------------------------------------------------------
# Binance is the only source here: one request per month, no weekend holes,
# and it is the market these prices actually formed in.
_CRYPTO = [
    ("BTCUSD", "Bitcoin", "BTCUSDT", 2017, 0.01, 2),
    ("ETHUSD", "Ethereum", "ETHUSDT", 2017, 0.01, 2),
    ("BNBUSD", "BNB", "BNBUSDT", 2017, 0.01, 2),
    ("SOLUSD", "Solana", "SOLUSDT", 2020, 0.001, 3),
    ("XRPUSD", "XRP", "XRPUSDT", 2018, 0.00001, 5),
    ("ADAUSD", "Cardano", "ADAUSDT", 2018, 0.00001, 5),
    ("DOGEUSD", "Dogecoin", "DOGEUSDT", 2019, 0.000001, 6),
    ("LTCUSD", "Litecoin", "LTCUSDT", 2017, 0.001, 3),
]
INSTRUMENTS += [
    Instrument(sym, name, "crypto", point, digits, "binance", code, year,
               contract=1.0)
    for sym, name, code, year, point, digits in _CRYPTO
]


# What history actually costs, so the download dialog can tell the truth
# before anyone commits to it. Megabytes are the stored size including every
# derived timeframe. All of these are measured, not guessed.
#
#   forex      41 pairs, 26 years each: 1.7 min and 260-336 MB per pair
#   metals     gold 2009-2026: 43 s and 226 MB
#   indices    Nasdaq 2024-2026: 82 s and 33 MB, i.e. 12.8 MB a year — the
#              same as forex, because the file is a full 24h series either way
#   dukascopy  one request per DAY, so ~250 requests a year: the slow one
#
# The floor matters more than the rate at small ranges. A HistData year is one
# page scrape, one POST, one zip and 370,000 lines to parse, and with four
# workers a three-year request is a single round — so three years and ten years
# cost far more similar amounts of time than a per-year rate suggests.
COST = {
    #  source        MB/year  seconds/year  floor
    "histdata_fx":    (12.8,     5,          25),
    "histdata_metal": (13.0,     5,          25),
    "histdata_index": (12.8,     5,          25),
    "binance":        (19.0,    25,          20),
    "dukascopy":      (13.0,   750,          60),
}


def cost_per_year(inst: "Instrument") -> tuple[float, float, float]:
    if inst.source == "dukascopy":
        key = "dukascopy"
    elif inst.source == "binance":
        key = "binance"
    elif inst.group == "forex":
        key = "histdata_fx"
    elif inst.group == "metals":
        key = "histdata_metal"
    else:
        key = "histdata_index"
    return COST[key]


BY_SYMBOL = {i.symbol: i for i in INSTRUMENTS}

GROUPS = ["forex", "metals", "indices", "energy", "crypto"]


def get(symbol: str) -> Instrument:
    try:
        return BY_SYMBOL[symbol.upper()]
    except KeyError:
        raise KeyError(f"unknown instrument {symbol!r}") from None
