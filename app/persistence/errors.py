class PersistenceError(Exception):
    """Base error raised by the persistence seam."""


class RecordAlreadyExists(PersistenceError):
    pass


class RecordNotFound(PersistenceError):
    pass


class ConcurrencyConflict(PersistenceError):
    pass
