"""Statistics for correlated time series."""
import math
import numpy as np

def mean_se(y) -> dict:
    """Mean, sample SD and the standard error of the mean of a correlated series.

    SE = SD / sqrt(n) * sqrt((1 + r) / (1 - r)) with r the lag-1 autocorrelation (AR(1) model, r clipped to
    [0, 0.95]); n_eff = n (1 - r) / (1 + r) is the number of effectively independent samples."""
    a = np.asarray([v for v in y if v is not None and math.isfinite(v)], float)
    n = a.size
    if n == 0:
        return {"n": 0, "mean": math.nan, "sd": math.nan, "se": math.nan, "r1": math.nan, "n_eff": 0.0}
    m = float(a.mean())
    if n < 3:
        sd = float(a.std(ddof=1)) if n > 1 else math.nan
        return {"n": n, "mean": m, "sd": sd, "se": sd / math.sqrt(n) if n > 1 else math.nan, "r1": math.nan, "n_eff": float(n)}
    d = a - m
    var = float((d * d).mean())
    r = float((d[1:] * d[:-1]).mean() / var) if var > 0 else 0.0
    r = min(max(r, 0.0), 0.95)
    sd = float(a.std(ddof=1))
    f = (1 + r) / (1 - r)
    return {"n": n, "mean": m, "sd": sd, "se": sd / math.sqrt(n) * math.sqrt(f), "r1": r, "n_eff": n / f}
