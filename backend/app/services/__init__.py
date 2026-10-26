"""Domain services: the deterministic layer between HTTP/tools and the models.

Nothing is re-exported here on purpose. Callers import the module they need
(``from app.services import scheduling``), which keeps cross-service
dependencies visible in the import block and avoids a package-level cycle.
"""
