"""Model-facing schema for the optional plugin-local crawl tool."""

CRAWL4AI_CRAWL_SCHEMA = {
    "name": "crawl4ai_crawl",
    "description": (
        "Crawl a bounded set of pages under one public website using the user's "
        "self-hosted Crawl4AI service. Use this only when several linked pages are "
        "needed; for one known page, use web_extract instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Public HTTP(S) seed URL to crawl.",
            },
            "depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "default": "basic",
                "description": "Basic follows one level; advanced follows up to two levels.",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 10,
                "description": "Maximum pages returned from the crawl.",
            },
        },
        "required": ["url"],
    },
}
