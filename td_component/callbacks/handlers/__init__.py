"""HTTP handler implementations, sliced by domain.

Each module in this package contains a contiguous slice of the original
``mcp_webserver_callbacks.py`` source (preserved in PR-16 byte-for-byte).
Cross-module imports are NOT used — at runtime everything flattens into a
single textDAT body via :func:`td_component.callbacks._composer.compose`.
"""
