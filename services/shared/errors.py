"""Error classification for ingestion (SPEC: Ingestion, "Failure handling").

Two kinds of failure:

  Permanent: retrying cannot help (invalid message, sha256 mismatch, unparseable PDF, a key
             that matches no stored version). Raised as PermanentError. The queue handler
             sends the message to the DLQ at once instead of retrying.
  Transient: everything else (throttling, timeouts, network errors, and any exception
             nobody classified). The handler lets it propagate, so SQS redelivers.

Transient is the default on purpose. Calling a transient error permanent loses an upload
with no retry; the opposite mistake only costs a few wasted attempts.
"""

# The reason travels as an SQS message attribute and lands in logs: keep it short.
MAX_REASON_LENGTH = 256


class PermanentError(Exception):
    """A failure that retrying cannot fix.

    code:    short, stable, machine-readable (e.g. "sha256_mismatch"); safe to alarm on.
    message: one line for a human. Never include document content or secrets: it is
             stored in last_error and sent to the DLQ as a message attribute.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def reason(self) -> str:
        """'code: message', capped in length, for last_error and the DLQ attribute."""
        return f"{self.code}: {self.message}"[:MAX_REASON_LENGTH]
