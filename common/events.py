"""The CogniKart event taxonomy and error-code enum.

Why a fixed taxonomy instead of free-text messages: the platform groups,
counts and trends on `event` and `errorCode`. Grouping on a stable enum is
deterministic; regexing a human-readable `message` is not. Every log line
carries exactly one event from this list.
"""

# --- Request lifecycle -----------------------------------------------------
HTTP_REQUEST_COMPLETED = "http.request.completed"
HTTP_REQUEST_FAILED = "http.request.failed"
SERVICE_HEARTBEAT = "service.heartbeat"
SERVICE_STARTED = "service.started"

# --- Catalog ---------------------------------------------------------------
CATALOG_LIST = "catalog.product.list"
CATALOG_VIEW = "catalog.product.view"
CATALOG_NOT_FOUND = "catalog.product.not_found"
CATALOG_STOCK_RESERVED = "catalog.stock.reserved"
CATALOG_STOCK_CONFLICT = "catalog.stock.conflict"
CATALOG_CACHE_DECISION = "catalog.cache.decision"  # DEBUG-level only

# --- Cart / checkout -------------------------------------------------------
CART_ITEM_ADDED = "cart.item.added"
CART_INVALID = "cart.invalid"
CHECKOUT_STARTED = "checkout.started"
CHECKOUT_CONFIRMED = "checkout.confirmed"
CHECKOUT_FAILED = "checkout.failed"

# --- Orders ----------------------------------------------------------------
ORDER_CREATED = "order.created"
ORDER_STOCK_CHECK = "order.stock.check"
ORDER_PAYMENT_AUTHORIZE_OK = "order.payment.authorize.ok"
ORDER_PAYMENT_AUTHORIZE_RETRY = "order.payment.authorize.retry"
ORDER_PAYMENT_AUTHORIZE_FAILED = "order.payment.authorize.failed"
ORDER_NOT_FOUND = "order.not_found"

# --- Payments --------------------------------------------------------------
PAYMENT_AUTHORIZED = "payment.authorize.ok"
PAYMENT_DECLINED = "payment.authorize.declined"
PAYMENT_UPSTREAM_ERROR = "payment.authorize.upstream_error"
PAYMENT_REFUNDED = "payment.refund.ok"

# --- Authentication & authorization ----------------------------------------
# These are what make sign-in activity visible in OpsMind. They carry a role
# and a hashed user id, never an email address.
AUTH_SIGNUP_OK = "auth.signup.ok"
AUTH_SIGNUP_FAILED = "auth.signup.failed"
AUTH_SIGNIN_OK = "auth.signin.ok"
AUTH_SIGNIN_FAILED = "auth.signin.failed"
AUTH_SIGNOUT = "auth.signout"
AUTHZ_DENIED = "authz.denied"

# --- Order lifecycle -------------------------------------------------------
ORDER_PLACED = "order.placed"
ORDER_STOCK_RESERVED = "order.stock.reserved"
ORDER_STOCK_RELEASED = "order.stock.released"
ORDER_PAYMENT_REQUESTED = "order.payment.requested"
ORDER_PAID = "order.paid"
ORDER_PAYMENT_REJECTED = "order.payment.rejected"
ORDER_FULFILLED = "order.fulfilled"
ORDER_CANCELLED = "order.cancelled"
ORDER_NOT_PAYABLE = "order.not_payable"

# --- Stock administration (staff) ------------------------------------------
STOCK_LEVEL_SET = "stock.level.set"
STOCK_AVAILABILITY_CHANGED = "stock.availability.changed"

# --- Chaos control (audit trail, not a detection signal) -------------------
CHAOS_APPLIED = "chaos.applied"
CHAOS_CLEARED = "chaos.cleared"
CHAOS_EXPIRED = "chaos.expired"

ALL_EVENTS = tuple(
    v for k, v in sorted(globals().items())
    if k.isupper() and isinstance(v, str) and "." in v and k != "ALL_EVENTS"
)


class ErrorCode:
    """Stable, groupable error codes. Mirrors `errorCode` in the log schema."""

    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    PAYMENT_DECLINED = "PAYMENT_DECLINED"
    PAYMENT_PROVIDER_ERROR = "PAYMENT_PROVIDER_ERROR"
    STOCK_CONFLICT = "STOCK_CONFLICT"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    EMPTY_CART = "EMPTY_CART"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    PRODUCT_UNAVAILABLE = "PRODUCT_UNAVAILABLE"
    ORDER_NOT_PAYABLE = "ORDER_NOT_PAYABLE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    BAD_CREDENTIALS = "BAD_CREDENTIALS"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    WEAK_PASSWORD = "WEAK_PASSWORD"


# Error codes that represent a client mistake (4xx) rather than a server
# fault (5xx). The platform uses this split so a bad product id does not
# count against the service's error budget.
CLIENT_ERROR_CODES = frozenset({
    ErrorCode.PRODUCT_NOT_FOUND,
    ErrorCode.ORDER_NOT_FOUND,
    ErrorCode.VALIDATION_ERROR,
    ErrorCode.EMPTY_CART,
    ErrorCode.PAYMENT_DECLINED,
    ErrorCode.INSUFFICIENT_STOCK,
    ErrorCode.PRODUCT_UNAVAILABLE,
    ErrorCode.ORDER_NOT_PAYABLE,
    ErrorCode.UNAUTHENTICATED,
    ErrorCode.FORBIDDEN,
    ErrorCode.BAD_CREDENTIALS,
    ErrorCode.EMAIL_TAKEN,
    ErrorCode.WEAK_PASSWORD,
})

# Payment outcomes a shopper can choose at checkout. This is a simulator
# control, exactly like a chaos scenario: it is recorded on the log line as
# `paymentOutcomeRequested` so the demo is auditable, and OpsMind must never
# read it for detection. A single chosen failure is one log line; an incident
# needs sustained failure, which is what the Scenario Lab produces.
PAYMENT_OUTCOMES = ("approved", "rejected", "slow")
