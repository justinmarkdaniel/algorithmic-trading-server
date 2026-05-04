"""ML model registry — kept deliberately empty in the public template.

Add your trained models here as a sibling pattern to `server/strategies/`:

    server/models/
      my_model_meta.json     # input feature names, normalization, hyperparams
      my_model.npz           # frozen weights — small enough to ship in image
      my_model.py            # loader + a `predict(df) -> dict` callable

The loader exposes a single function returning `{"proba": float, "class": int}`
or whatever schema your strategy expects. The active strategy then imports it
and calls it inside its `decide()` body — see `docs/MODELS.md` for an end-to-
end walkthrough using a tiny NumPy transformer.

Why pure NumPy / a frozen `.npz` rather than torch at runtime: production
inference here is single-row, low-cadence (1h or 5m). Loading torch on a
small ARM instance burns ~600 MB RAM and 20+ seconds of cold-start before the
first tick — a hand-rolled NumPy forward pass keeps the runtime image under
300 MB and eliminates GPU/torch as a deployment dependency. Train wherever
you like; export weights to `.npz`; ship NumPy.
"""
