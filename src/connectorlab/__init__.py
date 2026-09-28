"""A binary that shows what goes in and out of a connector.

The tool prints; it never simulates. Every request and response it renders was
produced by the real fetch path and read back off the adapter — see
``MockTransport.last_request`` / ``last_response``. A lab that built its own URL
would be a brochure, and nothing from outside could tell the difference.
"""
