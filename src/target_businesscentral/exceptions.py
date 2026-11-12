"""
target-businesscentral Exceptions
"""


class BusinessCentralError(Exception):
    """The base exception class for target-businesscentral.

    Parameters:
        msg (str): Short description of the error.
        response: Error response from the API call.
    """

    def __init__(self, msg, response=None):
        super(BusinessCentralError, self).__init__(msg)
        self.message = msg
        self.response = response

    def __str__(self):
        return self.message


class AuthenticationError(BusinessCentralError):
    """OAuth2 token request failed — bad client_id, secret, or scope."""


class TokenExpiredError(BusinessCentralError):
    """Bearer token expired mid-run — triggers re-authentication."""


class EntryPostError(BusinessCentralError):
    """POST to bc_gj_lines failed with a non-2xx response."""


class TransformError(BusinessCentralError):
    """CSV transformation failed — missing or invalid field values."""


class MappingConfigError(BusinessCentralError):
    """mapping_config.json is missing, malformed, or uses an unknown source."""


class RateLimitError(BusinessCentralError):
    """Business Central API returned HTTP 429."""


class ServerError(BusinessCentralError):
    """Business Central API returned HTTP 5xx."""
