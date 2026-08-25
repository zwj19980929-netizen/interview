from uuid import uuid4


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid4().hex[:16])

