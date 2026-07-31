"use client";

// Global display-currency preference: Home (each figure in its native
// currency), USD or EUR. Rates come once per session from /api/fx (macro
// warehouse spot rates). Conversion applies today's rate to every year of
// history — standard for display, and flagged as such wherever it's active.
//
// Components never convert by hand: `useMoney()` hands out formatters and a
// `convert` for chart series, all keyed by the value's HOME currency (derived
// from its jurisdiction or an explicit currency code via homeCurrency()).

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/lib/api";
import {
  CURRENCY_SYMBOL,
  homeCurrency,
  moneyC,
  perShareC,
  type CurrencyCode,
} from "@/lib/fmt";

export type CurrencyPref = "HOME" | "USD" | "EUR";
const PREF_KEY = "mzqa_display_currency";

type CurrencyContextValue = {
  pref: CurrencyPref;
  setPref: (p: CurrencyPref) => void;
  /** Units of currency per 1 USD; null until /api/fx answers (or if it never does). */
  rates: Record<string, number> | null;
  asOf: string | null;
};

const CurrencyContext = createContext<CurrencyContextValue>({
  pref: "HOME",
  setPref: () => {},
  rates: null,
  asOf: null,
});

export function CurrencyProvider({ children }: { children: ReactNode }) {
  const [pref, setPrefState] = useState<CurrencyPref>("HOME");
  const [rates, setRates] = useState<Record<string, number> | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(PREF_KEY);
      if (saved === "USD" || saved === "EUR") setPrefState(saved);
    } catch {
      /* storage unavailable */
    }
    api
      .fx()
      .then((fx) => {
        if (fx?.rates && typeof fx.rates.USD === "number") {
          setRates(fx.rates);
          setAsOf(fx.as_of || null);
        }
      })
      .catch(() => {
        /* no rates → the switcher stays hidden and everything renders in home currency */
      });
  }, []);

  const setPref = useCallback((p: CurrencyPref) => {
    setPrefState(p);
    try {
      if (p === "HOME") window.localStorage.removeItem(PREF_KEY);
      else window.localStorage.setItem(PREF_KEY, p);
    } catch {
      /* storage unavailable */
    }
  }, []);

  const value = useMemo(() => ({ pref, setPref, rates, asOf }), [pref, setPref, rates, asOf]);
  return <CurrencyContext.Provider value={value}>{children}</CurrencyContext.Provider>;
}

export function useCurrency(): CurrencyContextValue {
  return useContext(CurrencyContext);
}

export type MoneyKit = {
  pref: CurrencyPref;
  /** True when a non-home currency is selected AND rates are available. */
  active: boolean;
  /** "converted to USD at today's rate (as of …)" — null when showing home currency. */
  note: string | null;
  /** The currency a value natively in `homeLike` will be DISPLAYED in. */
  displayCode: (homeLike?: string | null) => CurrencyCode;
  /** Display symbol for values natively in `homeLike` ("$", "¥", "€"). */
  symbol: (homeLike?: string | null) => string;
  /** Convert one number from its home currency into the display currency. */
  convert: (v: number, homeLike?: string | null) => number;
  /** Compact money (e.g. "€1.4B") in the display currency. */
  money: (v: number | null | undefined, homeLike?: string | null) => string;
  /** Per-share money (e.g. "€3.47") in the display currency. */
  perShare: (v: number | null | undefined, homeLike?: string | null) => string;
};

/** Currency-aware money formatting. `homeLike` is a jurisdiction ("US"/"JP"/…)
 *  or a currency code ("USD"/"JPY"/"EUR") — whatever the call site has. */
export function useMoney(): MoneyKit {
  const { pref, rates, asOf } = useCurrency();

  return useMemo(() => {
    const rateOf = (c: CurrencyCode): number | null => {
      const r = rates?.[c];
      return typeof r === "number" && isFinite(r) && r > 0 ? r : null;
    };
    const displayCode = (homeLike?: string | null): CurrencyCode => {
      const home = homeCurrency(homeLike);
      if (pref === "HOME" || pref === home) return home;
      // Only convert when both legs of the rate exist.
      return rateOf(pref) !== null && rateOf(home) !== null ? pref : home;
    };
    const convert = (v: number, homeLike?: string | null): number => {
      const home = homeCurrency(homeLike);
      const to = displayCode(homeLike);
      if (to === home) return v;
      return (v * (rateOf(to) as number)) / (rateOf(home) as number);
    };
    const convertOpt = (v: number | null | undefined, homeLike?: string | null): number | null =>
      typeof v === "number" && isFinite(v) ? convert(v, homeLike) : null;

    const active = pref !== "HOME" && rateOf(pref) !== null;
    return {
      pref,
      active,
      note: active ? `converted to ${pref} at today's rate${asOf ? ` (${asOf})` : ""}` : null,
      displayCode,
      symbol: (homeLike) => CURRENCY_SYMBOL[displayCode(homeLike)],
      convert,
      money: (v, homeLike) => moneyC(convertOpt(v, homeLike), displayCode(homeLike)),
      perShare: (v, homeLike) => perShareC(convertOpt(v, homeLike), displayCode(homeLike)),
    };
  }, [pref, rates, asOf]);
}
