"""Directory-plugin entry point for direct Hermes installation."""

if __package__:
    # Hermes loads directory plugins as a namespaced package.
    from .hermes_web_crawl4ai import register
else:
    # Test runners may import a repository-root __init__.py as a plain module.
    from hermes_web_crawl4ai import register

__all__ = ["register"]
