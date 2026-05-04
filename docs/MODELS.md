# Adding an ML model

Models live in `server/models/`. They are imported by strategies, not by the
bot directly — a strategy is the consumer of a model's output, the same way it
would be the consumer of a TA indicator. This keeps the runtime architecture
simple: strategies are the only thing the dispatcher cares about, and a model
is just a function a strategy can call.

## The contract

A model module exports one callable:

```python
def predict(features: dict | np.ndarray | pd.DataFrame) -> dict:
    """Return whatever the strategy needs.

    Common shapes:
      {"proba_long": 0.62, "proba_short": 0.18, "class": "LONG"}
      {"forecast_p25": [<6 values>], "forecast_p50": [<6 values>], ...}
      {"score": 0.41}
    """
```

The strategy calls it inside `decide()` and combines the result with whatever
TA gating you want.

## Why pure NumPy at runtime

The default pattern in this repo is "train wherever, ship NumPy":

1. Train your model in your framework of choice.
2. Export the trained weights and any normalisation parameters to a `.npz`
   file plus a small `.json` metadata sidecar (input feature names, class
   labels, etc.).
3. Hand-roll the forward pass in NumPy, loading the `.npz` once at module
   import.

A small NumPy forward pass loads in ~50 ms on cold start, runs in microseconds
per row, and adds ~5 MB to the runtime image. Loading torch on a small ARM
instance burns ~600 MB RAM and 20+ seconds of cold-start before the first
tick. For low-cadence inference (1h or 5m) this is dominant overhead with
zero benefit — you're not getting any throughput out of a torch tensor for a
single-row prediction.

If your model genuinely needs torch at runtime (e.g. a foundation model you
can't reasonably re-implement), add `torch` and the framework deps to
`pyproject.toml` and copy the model checkpoint into the Dockerfile builder
stage. Expect the image to grow by hundreds of MB.

## Five-minute walkthrough — a tiny scoring model

Suppose you've trained a logistic regression that takes 5 features and returns
a long-probability. Here's the end-to-end pattern.

### 1. Export the trained model

After training in scikit-learn:

```python
import numpy as np
np.savez(
    "server/models/longscore.npz",
    weights=clf.coef_.astype(np.float32),     # shape (1, 5)
    bias=clf.intercept_.astype(np.float32),   # shape (1,)
    feature_mean=X_train.mean().values.astype(np.float32),
    feature_std=X_train.std().values.astype(np.float32),
)
import json
json.dump(
    {"feature_names": ["rsi14", "macdh", "atr14", "bbp", "willr14"],
     "version": "v1.0",
     "trained_on": "2026-04-15"},
    open("server/models/longscore_meta.json", "w"),
    indent=2,
)
```

### 2. Write the loader + predict()

`server/models/longscore.py`:

```python
"""Tiny logistic-regression long-score model.

Loaded once at import. predict() takes the latest indicator row from the
engine's TA-augmented dataframe and returns {"proba_long": float}.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_W = np.load(_HERE / "longscore.npz")
_META = json.loads((_HERE / "longscore_meta.json").read_text())

_FEATURES = _META["feature_names"]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def predict(row: pd.Series) -> dict:
    """Single-row prediction. Returns {'proba_long': float in [0, 1]}."""
    x = np.array([row.get(f, np.nan) for f in _FEATURES], dtype=np.float32)
    if np.isnan(x).any():
        return {"proba_long": None, "reason": "feature nan"}

    x = (x - _W["feature_mean"]) / np.where(_W["feature_std"] == 0, 1.0, _W["feature_std"])
    logit = _W["weights"] @ x + _W["bias"]
    return {"proba_long": float(_sigmoid(logit)[0])}
```

### 3. Use it from a strategy

`server/strategies/active/longscore_long.py`:

```python
from __future__ import annotations
import pandas as pd

from server.models.longscore import predict as longscore


def decide_longscore_long(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"action": "HOLD", "why": "warming"}

    out = longscore(df.iloc[-1])
    p = out.get("proba_long")
    if p is None:
        return {"action": "HOLD", "why": out.get("reason", "model nan")}

    if p >= 0.55:
        return {"action": "OPEN_LONG", "sl_pct": 0.005, "tp_pct": 0.010,
                "why": f"longscore p={p:.3f}"}
    return {"action": "HOLD", "why": f"longscore p={p:.3f} below threshold"}
```

Register it in `server/strategies/__init__.py` exactly like any other strategy
(see [STRATEGIES.md](STRATEGIES.md)).

### 4. Log every prediction

Strategies that wrap a model should also call
`STATE.record_model_prediction({...})` on every tick, regardless of whether
the prediction crosses the OPEN_LONG threshold. This populates the
`/model-predictions` endpoint, which is invaluable for offline threshold
tuning ("what WR would I get at p>=0.50 vs 0.55 vs 0.60?") without re-running
inference.

```python
from server.bot.state_store import STATE

STATE.record_model_prediction({
    "ts": str(df.index[-1]),
    "engine": "longscore_long",
    "proba": p,
    "row_features": {f: float(row.get(f, float("nan"))) for f in _FEATURES},
})
```

## What about heavier models

If you're shipping something larger (a transformer, a tree ensemble that needs
its training framework at runtime, etc.), the pattern is the same — only the
implementation of `predict()` changes:

- Tree ensembles: keep the trees in JSON, walk them in NumPy. Often much
  faster than the original library's Python API for single-row inference,
  and you skip the C++ runtime dependency entirely.
- Transformers: write the forward pass in NumPy (multi-head attention is
  ~30 lines), load weights from `.npz`. Skip torch at runtime.
- Foundation models you can't re-implement: vendor torch + the checkpoint
  into the Docker builder stage, accept the larger image.

The `models/__init__.py` docstring has the short version of this guidance.

## Testing models in isolation

Strategies should have a unit test that hand-builds a row, calls `decide()`,
and asserts the response — independent of the live engine. For models, add a
similar test that hand-builds a feature vector, calls `predict()`, and asserts
the output is in range:

```python
def test_longscore_predict_in_range():
    import pandas as pd
    from server.models.longscore import predict
    row = pd.Series({"rsi14": 30.0, "macdh": 0.0, "atr14": 50.0,
                     "bbp": 0.5, "willr14": -50.0})
    out = predict(row)
    assert out["proba_long"] is not None
    assert 0.0 <= out["proba_long"] <= 1.0
```
