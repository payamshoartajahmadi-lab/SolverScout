"""Local installed implementation catalog and versioned numeric adapter."""
from importlib import import_module

_EXPORTS = {
    "CatalogSnapshot": "models", "SolverCardV2": "models", "shortlist": "shortlist",
    "CATALOG_SCHEMA": "store", "CatalogStore": "store", "catalog_trial_request": "trials",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value
