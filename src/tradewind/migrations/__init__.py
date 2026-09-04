"""Tradewind SQL migrations (yoyo-migrations).

Not imported at runtime as Python -- these are data files read from
disk by the store adapters (`tradewind.adapters.sqlite_store`; a
Postgres adapter gets its own `postgres/` directory when P-4 lands).
This `__init__.py` only marks the directory as part of the `tradewind`
package for packaging purposes.
"""
